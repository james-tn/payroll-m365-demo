#!/usr/bin/env bash
# Rebuilds the image with ACR Tasks and rolls a new Container App revision.
# Assumes /tmp/azure_provision.env or local env has ACR_NAME set.
set -euo pipefail

if [[ -f /tmp/azure_provision.env ]]; then
  set -a; source /tmp/azure_provision.env; set +a
fi

: "${ACR_NAME:?ACR_NAME is required}"
RG="${RG:-payroll-m365-demo-rg}"
APP="${APP:-payroll-m365-demo}"
TAG="${TAG:-v$(date +%Y%m%d%H%M%S)}"

cd "$(dirname "$0")/.."

echo ">> Building image ${ACR_NAME}.azurecr.io/payroll-demo:${TAG} with ACR Tasks…"
az acr build --registry "$ACR_NAME" --image "payroll-demo:${TAG}" --file Dockerfile . >/dev/null
echo ">> Updating Container App revision…"
az containerapp update -g "$RG" -n "$APP" --image "${ACR_NAME}.azurecr.io/payroll-demo:${TAG}" -o none
FQDN=$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)
echo ">> Live at: https://${FQDN}"
echo ">> Health: $(curl -sS https://${FQDN}/health)"
