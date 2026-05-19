# send-oam-test.ps1
# Send an OAM test email from james.nguyen@microsoft.com to admin@MngEnvMCAP683110.onmicrosoft.com
#
# Prereqs:
#   Install-Module Microsoft.Graph -Scope CurrentUser -Force
#
# Usage:
#   1. Edit $Originator below to the GUID you generated in the OAM dashboard
#   2. pwsh ./send-oam-test.ps1
#   3. A browser pops up to sign in as james.nguyen@microsoft.com
#      (consent to "Mail.Send" the first time)

$From       = "james.nguyen@microsoft.com"
$To         = "admin@MngEnvMCAP683110.onmicrosoft.com"
$Subject    = "🔔 PayCycle OAM test (from microsoft.com) · 2 payroll exceptions"
$BaseUrl    = "https://payroll-m365-demo.politeground-c0ea36c5.eastus2.azurecontainerapps.io"

# >>>>> EDIT THIS to the GUID you registered in the OAM dashboard <<<<<
$Originator = "REPLACE-WITH-NEW-ORIGINATOR-GUID"

# Build the Adaptive Card JSON (must be plain ASCII for embedding)
$card = @{
    type = "AdaptiveCard"
    '$schema' = "http://adaptivecards.io/schemas/adaptive-card.json"
    version = "1.5"
    originator = "817b732b-ff83-462a-ba2e-fcc58c9b3b3c"
    hideOriginalBody = $false
    body = @(
        @{ type = "TextBlock"; text = "Payroll exceptions need your review"; size = "Large"; weight = "Bolder"; wrap = $true }
        @{ type = "TextBlock"; text = "Acme Manufacturing - Pay period ending May 30, 2026"; isSubtle = $true; wrap = $true; spacing = "None" }
        @{
            type = "FactSet"
            spacing = "Medium"
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
            type = "Action.Http"
            title = "Approve both & submit batch"
            method = "POST"
            url = "$BaseUrl/cta/approve?token=manual-test-token"
            body = '{"verb":"approve_all_and_submit","batch_id":"BATCH-2026-05B"}'
            headers = @(
                @{ name = "Content-Type"; value = "application/json" }
            )
        }
        @{
            type = "Action.OpenUrl"
            title = "Review with PayCycle Assistant"
            url = "$BaseUrl/cta/handoff?token=manual-test-token&surface=teams"
        }
    )
}

$cardJson = ($card | ConvertTo-Json -Depth 12 -Compress)

# HTML body. The <script type="application/adaptivecard+json"> tag in <head>
# is the OAM payload. The <body> is the fallback for clients that don't render OAM.
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

# Connect to Graph and send
Connect-MgGraph -Scopes "Mail.Send" -NoWelcome

$message = @{
    Message = @{
        Subject = $Subject
        Body = @{ ContentType = "HTML"; Content = $html }
        ToRecipients = @(
            @{ EmailAddress = @{ Address = $To } }
        )
    }
    SaveToSentItems = $true
}

Send-MgUserMail -UserId $From -BodyParameter $message
Write-Host "Sent to $To. Check inbox in ~30 seconds." -ForegroundColor Green
Disconnect-MgGraph | Out-Null
