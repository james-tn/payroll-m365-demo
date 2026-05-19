#!/usr/bin/env bash
# End-to-end Azure provisioning for the PayCycle M365 demo.
#
# Provisions (in order, idempotent where Azure allows):
#   1. Resource group
#   2. User-Assigned Managed Identity (for the bot federated identity)
#   3. Entra ID app registration for the bot (no secret - UAMI federation)
#   4. Azure Communication Services + Email service + Azure managed domain
#   5. Azure Container Registry
#   6. Container Apps environment
#   7. Container App (initial placeholder image; deploy.sh rolls real revisions)
#   8. Azure Bot Service (MultiTenant; messaging endpoint -> the Container App)
#   9. Token signing key (random; written to Container App secrets)
#
# Writes everything operational to /tmp/azure_provision.env so subsequent runs
# of deploy.sh can find ACR_NAME, RG, APP, etc.
#
# Reuses Azure OpenAI from outside this script - set AZURE_OPENAI_ENDPOINT
# and AZURE_OPENAI_KEY (or AZURE_OPENAI_DEPLOYMENT) as env vars before running
# if you have an existing instance. Otherwise the script skips OpenAI wiring
# and you can add it later.
#
# Usage:
#   bash infrastructure/provision.sh                       # use defaults
#   RG=my-rg LOCATION=westus2 bash infrastructure/provision.sh
#
# Required tools: az (>=2.55), jq, openssl.

set -euo pipefail

# ---- Configuration (override via env) ----
RG="${RG:-payroll-m365-demo-rg}"
LOCATION="${LOCATION:-eastus2}"
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-}"

UAMI_NAME="${UAMI_NAME:-payroll-bot-uami}"
BOT_APP_NAME="${BOT_APP_NAME:-payroll-m365-demo-bot}"
BOT_DISPLAY_NAME="${BOT_DISPLAY_NAME:-PayCycle Assistant}"
ACS_NAME="${ACS_NAME:-paycycle-acs-$RANDOM}"
EMAIL_SVC_NAME="${EMAIL_SVC_NAME:-paycycle-email-svc}"
ACR_NAME="${ACR_NAME:-payrollm365demo$(shuf -i 1000-9999 -n 1)}"
CAE_NAME="${CAE_NAME:-payroll-cae}"
APP_NAME="${APP_NAME:-payroll-m365-demo}"
APP_PORT="${APP_PORT:-8080}"
INITIAL_IMAGE="${INITIAL_IMAGE:-mcr.microsoft.com/k8se/quickstart:latest}"

OUT_ENV="${OUT_ENV:-/tmp/azure_provision.env}"

# ---- Pre-flight ----
command -v az >/dev/null || { echo "ERROR: az CLI is required"; exit 1; }
command -v jq >/dev/null || { echo "ERROR: jq is required"; exit 1; }
command -v openssl >/dev/null || { echo "ERROR: openssl is required"; exit 1; }

if [[ -z "$SUBSCRIPTION_ID" ]]; then
  SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
fi
TENANT_ID="$(az account show --query tenantId -o tsv)"
echo ">> Subscription: $SUBSCRIPTION_ID"
echo ">> Tenant:       $TENANT_ID"
echo ">> Region:       $LOCATION"
echo ">> Resource grp: $RG"
echo

# Ensure required providers are registered (no-op if already registered)
for ns in Microsoft.App Microsoft.OperationalInsights Microsoft.ContainerRegistry \
          Microsoft.BotService Microsoft.Communication Microsoft.ManagedIdentity; do
  state="$(az provider show -n "$ns" --query registrationState -o tsv 2>/dev/null || echo NotRegistered)"
  if [[ "$state" != "Registered" ]]; then
    echo ">> Registering provider $ns ..."
    az provider register -n "$ns" --wait
  fi
done

# ---- 1. Resource group ----
echo ">> [1/9] Resource group ($RG)"
az group create -n "$RG" -l "$LOCATION" -o none

# ---- 2. UAMI ----
echo ">> [2/9] User-Assigned Managed Identity ($UAMI_NAME)"
az identity create -g "$RG" -n "$UAMI_NAME" -l "$LOCATION" -o none
UAMI_JSON="$(az identity show -g "$RG" -n "$UAMI_NAME" -o json)"
UAMI_CLIENT_ID="$(echo "$UAMI_JSON"   | jq -r .clientId)"
UAMI_PRINCIPAL_ID="$(echo "$UAMI_JSON" | jq -r .principalId)"
UAMI_RESOURCE_ID="$(echo "$UAMI_JSON"  | jq -r .id)"

# ---- 3. Bot Entra app + federated credential ----
echo ">> [3/9] Bot Entra app registration ($BOT_APP_NAME, multi-tenant)"
EXISTING_APP_ID="$(az ad app list --display-name "$BOT_APP_NAME" --query "[0].appId" -o tsv 2>/dev/null || true)"
if [[ -z "$EXISTING_APP_ID" ]]; then
  BOT_APP_ID="$(az ad app create \
      --display-name "$BOT_APP_NAME" \
      --sign-in-audience AzureADMultipleOrgs \
      --query appId -o tsv)"
  az ad sp create --id "$BOT_APP_ID" -o none
else
  BOT_APP_ID="$EXISTING_APP_ID"
fi
BOT_APP_OID="$(az ad app show --id "$BOT_APP_ID" --query id -o tsv)"

# Federated identity credential: trust the UAMI to act as this bot app
FIC_NAME="payroll-bot-uami-fic"
if ! az ad app federated-credential show --id "$BOT_APP_ID" --federated-credential-id "$FIC_NAME" >/dev/null 2>&1; then
  az ad app federated-credential create --id "$BOT_APP_ID" --parameters "{
    \"name\":\"$FIC_NAME\",
    \"issuer\":\"https://login.microsoftonline.com/${TENANT_ID}/v2.0\",
    \"subject\":\"$UAMI_PRINCIPAL_ID\",
    \"audiences\":[\"api://AzureADTokenExchange\"]
  }" -o none
fi

# ---- 4. ACS + Email + Azure managed domain ----
echo ">> [4/9] Azure Communication Services ($ACS_NAME) + Email ($EMAIL_SVC_NAME)"
az communication create -g "$RG" -n "$ACS_NAME" -l global --data-location UnitedStates -o none
ACS_CONNECTION_STRING="$(az communication list-key -g "$RG" -n "$ACS_NAME" --query primaryConnectionString -o tsv)"

az communication email create -g "$RG" -n "$EMAIL_SVC_NAME" -l global --data-location UnitedStates -o none
az communication email domain create -g "$RG" --email-service-name "$EMAIL_SVC_NAME" \
    --name AzureManagedDomain --location global --domain-management AzureManaged -o none

SENDER_DOMAIN="$(az communication email domain show -g "$RG" --email-service-name "$EMAIL_SVC_NAME" \
    --name AzureManagedDomain --query "properties.fromSenderDomain" -o tsv)"
ACS_SENDER_ADDRESS="DoNotReply@${SENDER_DOMAIN}"

# Link the email domain to the ACS resource
DOMAIN_ID="$(az communication email domain show -g "$RG" --email-service-name "$EMAIL_SVC_NAME" \
    --name AzureManagedDomain --query id -o tsv)"
az communication update -g "$RG" -n "$ACS_NAME" --linked-domains "$DOMAIN_ID" -o none

# ---- 5. ACR ----
echo ">> [5/9] Container Registry ($ACR_NAME)"
az acr create -g "$RG" -n "$ACR_NAME" --sku Basic --admin-enabled false -o none

# Grant the UAMI AcrPull on this registry
ACR_ID="$(az acr show -g "$RG" -n "$ACR_NAME" --query id -o tsv)"
az role assignment create --assignee-object-id "$UAMI_PRINCIPAL_ID" \
    --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID" \
    -o none 2>/dev/null || true

# ---- 6. Container Apps environment ----
echo ">> [6/9] Container Apps environment ($CAE_NAME)"
az containerapp env create -g "$RG" -n "$CAE_NAME" -l "$LOCATION" -o none

# ---- 7. Container App ----
echo ">> [7/9] Container App ($APP_NAME)"
TOKEN_SIGNING_KEY="$(openssl rand -hex 32)"
az containerapp create -g "$RG" -n "$APP_NAME" \
    --environment "$CAE_NAME" \
    --image "$INITIAL_IMAGE" \
    --target-port "$APP_PORT" --ingress external \
    --user-assigned "$UAMI_RESOURCE_ID" \
    --registry-server "${ACR_NAME}.azurecr.io" --registry-identity "$UAMI_RESOURCE_ID" \
    --min-replicas 1 --max-replicas 1 \
    --secrets \
        "acs-connection-string=$ACS_CONNECTION_STRING" \
        "token-signing-key=$TOKEN_SIGNING_KEY" \
    --env-vars \
        "ACS_CONNECTION_STRING=secretref:acs-connection-string" \
        "ACS_SENDER_ADDRESS=$ACS_SENDER_ADDRESS" \
        "TOKEN_SIGNING_KEY=secretref:token-signing-key" \
        "BOT_APP_ID=$BOT_APP_ID" \
        "BOT_TENANT_ID=$TENANT_ID" \
        "BOT_IDENTITY_TYPE=UserAssignedMSI" \
        "BOT_UAMI_CLIENT_ID=$UAMI_CLIENT_ID" \
        "DEMO_USER_EMAIL=${DEMO_USER_EMAIL:-}" \
        "DEMO_ADMIN_EMAIL=${DEMO_ADMIN_EMAIL:-}" \
        "DEMO_MANAGER_EMAIL=${DEMO_MANAGER_EMAIL:-}" \
    -o none

APP_FQDN="$(az containerapp show -g "$RG" -n "$APP_NAME" \
    --query properties.configuration.ingress.fqdn -o tsv)"
APP_BASE_URL="https://${APP_FQDN}"

# ---- 8. Azure Bot Service ----
echo ">> [8/9] Azure Bot Service ($BOT_APP_NAME)"
az bot create -g "$RG" -n "$BOT_APP_NAME" --kind azurebot \
    --app-type UserAssignedMSI --appid "$BOT_APP_ID" \
    --tenant-id "$TENANT_ID" --msi-resource-id "$UAMI_RESOURCE_ID" \
    --endpoint "${APP_BASE_URL}/api/messages" \
    --display-name "$BOT_DISPLAY_NAME" --sku F0 -o none 2>/dev/null || \
  az bot update -g "$RG" -n "$BOT_APP_NAME" \
    --endpoint "${APP_BASE_URL}/api/messages" -o none

# Enable Teams + M365 Copilot channels
az bot msteams create -g "$RG" -n "$BOT_APP_NAME" -o none 2>/dev/null || true

# ---- 9. Output env file ----
echo ">> [9/9] Writing $OUT_ENV"
cat > "$OUT_ENV" <<EOF
SUBSCRIPTION_ID=$SUBSCRIPTION_ID
TENANT_ID=$TENANT_ID
RG=$RG
LOCATION=$LOCATION
SENDER_DOMAIN=$SENDER_DOMAIN
ACS_CONNECTION_STRING=$ACS_CONNECTION_STRING
ACS_SENDER_ADDRESS=$ACS_SENDER_ADDRESS
BOT_APP_ID=$BOT_APP_ID
BOT_APP_OID=$BOT_APP_OID
BOT_NAME=$BOT_APP_NAME
ACR_NAME=$ACR_NAME
TOKEN_SIGNING_KEY=$TOKEN_SIGNING_KEY
APP_BASE_URL=$APP_BASE_URL
APP_FQDN=$APP_FQDN
APP=$APP_NAME
UAMI_CLIENT_ID=$UAMI_CLIENT_ID
UAMI_RESOURCE_ID=$UAMI_RESOURCE_ID
UAMI_PRINCIPAL_ID=$UAMI_PRINCIPAL_ID
UAMI_TENANT_ID=$TENANT_ID
EOF
chmod 600 "$OUT_ENV"

cat <<EOF

================================================================
✅ Provisioning complete.

Container App URL:    $APP_BASE_URL
Demo console:         $APP_BASE_URL/demo/console
Bot endpoint:         $APP_BASE_URL/api/messages
ACS sender address:   $ACS_SENDER_ADDRESS
Env file written to:  $OUT_ENV

Next steps:
  1. Update manifests/m365/manifest.json - set "id" and "bots[].botId"
     to: $BOT_APP_ID
  2. Build the manifest zip:
        cd manifests/m365 && zip -j payroll-demo-app.zip manifest.json *.png
  3. (Optional) Set Azure OpenAI vars on the Container App:
        az containerapp update -g $RG -n $APP_NAME --set-env-vars \\
          AZURE_OPENAI_ENDPOINT=https://<your>.openai.azure.com \\
          AZURE_OPENAI_KEY=<key> AZURE_OPENAI_DEPLOYMENT=<deployment>
  4. Build + roll the real image:
        bash infrastructure/deploy.sh
  5. Sideload the Teams app in your tenant (Apps -> Upload custom).
     The conversationUpdate fired on install captures the conv ref
     automatically - no "hi" needed.
================================================================
EOF
