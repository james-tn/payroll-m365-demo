# send-oam-test-devicecode.ps1
# Send an OAM test email from james.nguyen@microsoft.com to admin@MngEnvMCAP683110.onmicrosoft.com
# using device-code OAuth against the well-known public Microsoft Graph PowerShell client.
#
# No module install required. No admin approval required.
# Run:
#   pwsh -ExecutionPolicy Bypass -File .\send-oam-test-devicecode.ps1
#
# A device-code URL will print; open it in a browser and sign in as james.nguyen@microsoft.com.
# Consent to "Send mail as you" if prompted (user-level consent, no admin needed).

$From       = "james.nguyen@microsoft.com"
$To         = "admin@MngEnvMCAP683110.onmicrosoft.com"
$Subject    = "PayCycle OAM test (from microsoft.com) - 2 payroll exceptions"
$BaseUrl    = "https://payroll-m365-demo.politeground-c0ea36c5.eastus2.azurecontainerapps.io"

# >>>>> EDIT THIS to the GUID you registered in the OAM dashboard <<<<<
$Originator = "817b732b-ff83-462a-ba2e-fcc58c9b3b3c"

# Public Microsoft Graph PowerShell app - exists in every tenant, no admin install needed
$ClientId = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
$Tenant   = "72f988bf-86f1-41af-91ab-2d7cd011db47"

# --- Build Adaptive Card ---
$card = @{
    type = "AdaptiveCard"
    '$schema' = "http://adaptivecards.io/schemas/adaptive-card.json"
    version = "1.5"
    originator = $Originator
    hideOriginalBody = $false
    body = @(
        @{ type = "TextBlock"; text = "Payroll exceptions need your review"; size = "Large"; weight = "Bolder"; wrap = $true }
        @{ type = "TextBlock"; text = "Acme Manufacturing - Pay period ending May 30, 2026"; isSubtle = $true; wrap = $true; spacing = "None" }
        @{
            type = "FactSet"; spacing = "Medium"
            facts = @(
                @{ title = "Cycle"; value = "Pay period ending May 30, 2026" }
                @{ title = "Deadline"; value = "2026-05-30 17:00 PT" }
                @{ title = "Open exceptions"; value = "2" }
                @{ title = "Estimated impact"; value = "`$2,087.50" }
            )
        }
        @{
            type = "Container"; style = "warning"; spacing = "Medium"
            items = @(
                @{ type = "TextBlock"; text = "Joseph Smith - Overtime variance high"; weight = "Bolder"; wrap = $true }
                @{ type = "TextBlock"; text = "EXC-2026-05B-001 - 14.5h OT vs 4h avg (261% over 6 periods). Impact `$847.50."; wrap = $true; spacing = "Small" }
            )
        }
        @{
            type = "Container"; style = "accent"; spacing = "Small"
            items = @(
                @{ type = "TextBlock"; text = "Sarah Lee - PTO missing manager approval"; weight = "Bolder"; wrap = $true }
                @{ type = "TextBlock"; text = "EXC-2026-05B-002 - 3 days PTO 2026-05-25..27, no approver action. Impact `$1,240.00."; wrap = $true; spacing = "Small" }
            )
        }
    )
    actions = @(
        @{
            type = "Action.Http"; title = "Approve both & submit batch"; method = "POST"
            url = "$BaseUrl/cta/approve?token=manual-test-token"
            body = '{"verb":"approve_all_and_submit","batch_id":"BATCH-2026-05B"}'
            headers = @(@{ name = "Content-Type"; value = "application/json" })
        }
        @{
            type = "Action.OpenUrl"; title = "Review with PayCycle Assistant"
            url = "$BaseUrl/cta/handoff?token=manual-test-token&surface=teams"
        }
    )
}
$cardJson = ($card | ConvertTo-Json -Depth 12 -Compress)

$html = @"
<!DOCTYPE html><html><head><meta charset="utf-8">
<script type="application/adaptivecard+json">
$cardJson
</script>
</head>
<body style="font-family:Segoe UI,Arial,sans-serif;max-width:600px;color:#222">
  <h2 style="color:#c43d3d">Payroll exceptions need your review</h2>
  <p>Acme Manufacturing - Pay period ending May 30, 2026 - 2 open exceptions, est. impact `$2,087.50</p>
  <ul>
    <li><b>Joseph Smith</b> - Overtime variance high (`$847.50)</li>
    <li><b>Sarah Lee</b> - PTO missing manager approval (`$1,240.00)</li>
  </ul>
  <p><a href="$BaseUrl/cta/handoff?token=manual-test-token&surface=teams"
        style="display:inline-block;padding:10px 18px;background:#0078d4;color:white;text-decoration:none;border-radius:4px">
        Review with PayCycle Assistant
     </a></p>
</body></html>
"@

# --- Device-code OAuth ---
Write-Host "Requesting device code..." -ForegroundColor Cyan
$dc = Invoke-RestMethod -Method POST `
    -Uri "https://login.microsoftonline.com/$Tenant/oauth2/v2.0/devicecode" `
    -Body @{ client_id = $ClientId; scope = "Mail.Send offline_access openid profile" }

Write-Host "`n=========================================================" -ForegroundColor Yellow
Write-Host $dc.message -ForegroundColor Yellow
Write-Host "=========================================================`n" -ForegroundColor Yellow

# Poll
$token = $null
$deadline = (Get-Date).AddSeconds($dc.expires_in)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds $dc.interval
    try {
        $r = Invoke-RestMethod -Method POST `
            -Uri "https://login.microsoftonline.com/$Tenant/oauth2/v2.0/token" `
            -Body @{
                grant_type  = "urn:ietf:params:oauth:grant-type:device_code"
                client_id   = $ClientId
                device_code = $dc.device_code
            }
        $token = $r.access_token
        break
    } catch {
        $err = ($_.ErrorDetails.Message | ConvertFrom-Json -ErrorAction SilentlyContinue).error
        if ($err -and $err -ne "authorization_pending" -and $err -ne "slow_down") {
            Write-Host "Auth error: $err" -ForegroundColor Red
            exit 1
        }
    }
}
if (-not $token) { Write-Host "Timed out waiting for sign-in." -ForegroundColor Red; exit 1 }
Write-Host "Got token. Sending mail..." -ForegroundColor Green

# --- Send via Graph ---
$payload = @{
    message = @{
        subject      = $Subject
        body         = @{ contentType = "HTML"; content = $html }
        toRecipients = @(@{ emailAddress = @{ address = $To } })
    }
    saveToSentItems = $true
} | ConvertTo-Json -Depth 12

try {
    Invoke-RestMethod -Method POST `
        -Uri "https://graph.microsoft.com/v1.0/me/sendMail" `
        -Headers @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" } `
        -Body $payload
    Write-Host "Sent to $To. Check inbox in ~30 seconds." -ForegroundColor Green
} catch {
    Write-Host "Send failed: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.ErrorDetails) { Write-Host $_.ErrorDetails.Message -ForegroundColor Red }
    exit 1
}
