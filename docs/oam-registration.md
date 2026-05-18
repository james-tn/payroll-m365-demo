# Registering the sender for Outlook Actionable Messages (OAM)

Without this one-time registration the Adaptive Card will render inside Outlook
but its action buttons will be **disabled**. Microsoft's anti-phishing
protection requires that any sender of an actionable email is registered as an
*Outlook Actionable Message originator*.

This is a manual portal step; there is no CLI today.

## What you need

| Value | This demo |
|---|---|
| Sender mailbox | `DoNotReply@8cd3731a-c37e-4ac9-88d0-876dbcf5c3de.azurecomm.net` |
| Target users (Test Users mode) | `janguy@microsoft.com` |
| Public app/website url | `https://github.com/james-tn/payroll-m365-demo` |

> The sender is an **Azure Communication Services Email** Azure-managed domain.
> It is allowed as an originator just like any other mailbox.

## Steps

1. Go to <https://outlook.office.com/connectors/oam/publish> and sign in with
   the account you want to be the *originator owner*. Any AAD account is fine.

2. Click **New Provider** and fill in:

   | Field | Value |
   |---|---|
   | Provider Name | `PayCycle Payroll Demo` |
   | Sender email addresses from which actionable emails will originate | `DoNotReply@8cd3731a-c37e-4ac9-88d0-876dbcf5c3de.azurecomm.net` |
   | Target URLs of actions (one per line) | `https://payroll-m365-demo.politeground-c0ea36c5.eastus2.azurecontainerapps.io/cta/` (must be HTTPS, the prefix is enough) |
   | Scope of submission | **Test Users** (fastest — auto-approved within minutes) |
   | Test user email addresses | `janguy@microsoft.com` |
   | Provider Name (logo, public site) | upload anything, e.g. the GitHub repo URL |

3. Submit. You will get an **Originator ID** that looks like
   `8e85d570-4af1-4f37-89a2-7e0b2eee2c4f`.

4. Set it on the Container App:

   ```bash
   az containerapp update -g payroll-m365-demo-rg -n payroll-m365-demo \
     --set-env-vars OAM_ORIGINATOR_ID="<paste-guid-here>"
   ```

5. Re-trigger an email from the demo console and the **Approve / Reject /
   Discuss in Teams** buttons will now be active inside Outlook.

## How to tell if it worked

- Open the email. Hover over **Approve**. Outlook will show a *secure*
  preview, not a "this sender hasn't been verified" warning.
- Clicking the button shows an inline success card in the email body (the
  service responds with `CARD-UPDATE-IN-BODY: true`).
- If you click **Discuss in Teams** you will be redirected to Teams and a
  follow-up Adaptive Card will be waiting for you in the chat with the bot.
