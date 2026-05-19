# Actionable Email + Teams Hand-off — Customer Admin Setup Guide

This guide describes how to configure your tenant so that customers receive the
**full actionable-email experience** for notifications from an external service,
and have the email-to-Teams hand-off work seamlessly.

The pattern described here is **generic**: it applies to any service that sends
email notifications and wants to offer (a) inline approve/reject actions inside
Outlook, and (b) a "Review with Assistant" button that opens a Teams chat with
the email's context already loaded.

---

## 1. Two delivery modes

Every notification email is delivered in one of two modes, selected based on
how much customer-side configuration is in place:

### Mode A — Full actionable card

Email arrives with an **inline Adaptive Card** rendered above the HTML body.
The card surfaces all the key information (employees, amounts, exceptions, etc.)
and exposes **per-row action buttons** ("Approve", "Flag for HR", etc.). Clicking
a button POSTs to the service from inside Outlook and shows a confirmation
banner without ever opening a browser tab.

A "Review with Assistant" link at the bottom hands off to a Teams chat with the
email's context pre-loaded.

**Prerequisites**: this guide's steps 3–7 must be completed.

### Mode B — HTML email + Teams hand-off

If Mode A prerequisites are not met (or the recipient's client doesn't support
Adaptive Cards — e.g. Gmail, Apple Mail, Outlook mobile), the email arrives as
a **rich HTML message** with the same information, plus a "Review with
Assistant" CTA button. Clicking the CTA opens a Teams chat where the same
context is delivered to the assistant.

**Prerequisites**: **none.** Works out of the box, in any modern email client,
with no customer-tenant configuration.

This is the default / fallback experience and is always supported.

---

## 2. Decide which modes to enable

| Audience                                | Recommendation                                                                 |
| --------------------------------------- | ------------------------------------------------------------------------------ |
| Mixed mail clients (Gmail, mobile, etc.) | Mode B only — no setup needed.                                                 |
| Microsoft 365 customers                 | Mode A + Mode B fallback. ~30 min one-time setup, big UX win in Outlook.       |
| Internal pilot / single tenant          | Mode A in test scope (no Microsoft submission needed).                         |
| GA / many tenants                       | Mode A in global scope (requires Microsoft review, ~2 weeks).                  |

Mode A and Mode B coexist — the same email contains both the Adaptive Card
payload (rendered if the client supports it) and the HTML body (rendered
otherwise). No customer ever experiences both simultaneously; clients fall
back to HTML when card rendering isn't available.

---

## 3. Register the service as an Actionable Messages provider

A provider is the trust binding between the service's sender domain and the
backend that processes button clicks. One provider per sending product.

1. Open the **Actionable Email Developer Dashboard** at
   <https://outlook.office.com/connectors/oam/publish> while signed in as the
   sender's mailbox owner (or a tenant admin).
2. **New Provider** and fill in:
   - **Friendly Name** — internal label.
   - **Sender email address** — the mailbox the service sends from. Must
     match the `From:` of every notification. Multiple addresses are allowed.
   - **Target URLs** — base URL(s) where the service's API lives. Used as
     prefix matches; for example, `https://api.example.com` covers
     `https://api.example.com/cta/approve/<id>` etc.
   - **Is this a Power Automate Scenario?** — usually **No**.
   - **MsEntra Auth section** — see step 4.
3. Save the provider and copy:
   - **Provider Id (originator)** — a GUID. The service must embed this in
     every Adaptive Card it sends (`"originator": "<GUID>"`).
   - **App Id Uri** — auto-generated, format
     `api://auth-am-<provider-guid>/<entra-app-id>`. Used in step 4.

---

## 4. Register an Entra ID application for the service

Outlook authenticates each button click by sending a bearer token to the
service. The token is issued by Microsoft Entra ID for an application that
represents your service.

1. In the Microsoft Entra admin center, go to **Identity > Applications >
   App registrations > New registration**.
2. **Supported account types**:
   - **Accounts in any organizational directory (Multitenant)** for a provider
     intended for many customer tenants (Global scope).
   - **Accounts in this organizational directory only** for single-tenant /
     test scope.
3. Leave Redirect URI blank. Register.
4. Copy the **Application (client) ID** and paste it back into the OAM
   provider's `MsEntra Application Id` field. Save the provider; this causes
   the dashboard to generate the **App Id Uri** (step 3).
5. Go to **Expose an API** on the Entra app:
   - **Application ID URI** — set to the App Id Uri from the OAM provider.
   - **Add a scope**:
     - **Scope name**: anything, e.g. `Actions.Invoke`.
     - **Who can consent**: Admins and users (or Admins only — more
       restrictive but cleaner).
     - Save.
   - **Add a client application**:
     - **Client ID**: `48af08dc-f6d2-435f-b2a7-069abd99c086` (the Microsoft
       Actionable Messages service).
     - Check the scope you just created. Authorize.

This pre-authorization tells Entra: "the Actions service is allowed to
acquire tokens for my app without asking the user to consent."

---

## 5. Validate the bearer token on every action call

Each click POSTs to a service URL (under the registered Target URL) with an
`Authorization: Bearer <jwt>` header. The service **must** validate the token
before performing the action.

Validation steps:

1. Decode the JWT header to get the `kid` (key id).
2. Read the unverified `tid` (tenant id) claim — this is the user's tenant.
3. Fetch the OIDC discovery document for that tenant:
   `https://login.microsoftonline.com/<tid>/v2.0/.well-known/openid-configuration`
   then fetch the `jwks_uri` and look up the public key matching `kid`.
4. Validate the JWT signature with that key.
5. Validate `iss == https://login.microsoftonline.com/<tid>/v2.0`.
6. Validate `aud` equals either the Entra **Application (client) ID** (if the
   app's `requestedAccessTokenVersion` is 2) or the **App Id Uri** (version 1).
7. The `sub` / `oid` / `preferred_username` claims identify the user who
   clicked — use these for audit logs.

Cache the JWKS for at least an hour. Microsoft Entra rotates signing keys but
not faster than that.

**Reference implementation**: see `src/common/oam_auth.py` in this repository.

---

## 6. Server response shape

When the click succeeds, return HTTP 200 with these headers so Outlook updates
the card inline:

```
HTTP/1.1 200 OK
CARD-ACTION-STATUS: ✓ Approved
CARD-UPDATE-IN-BODY: true
Content-Type: application/json

{ "type": "AdaptiveCard", ... refresh card payload ... }
```

- `CARD-ACTION-STATUS` — short status text shown as a toast above the card.
- `CARD-UPDATE-IN-BODY` — instructs Outlook to replace the card body with the
  returned JSON. Omit this header if you don't want to refresh the card.

On failure, return 4xx/5xx with `CARD-ACTION-STATUS` containing a brief error
message. Outlook displays it as a red banner.

---

## 7. Submit / consent the provider

The provider scope determines the rollout path:

| Scope        | Audience                                      | Approval                                                              |
| ------------ | --------------------------------------------- | --------------------------------------------------------------------- |
| **Test**     | Specific mailboxes in the sender's tenant     | Auto-approved on save. Recipients must be in the test users list.     |
| **Org**      | All mailboxes in one named tenant             | ~24 hour rollout after Microsoft approval.                            |
| **Global**   | Any tenant whose admin consents               | ~2 week Microsoft review.                                             |

For **Global** scope, each customer tenant must also approve the Entra
application:

1. Tenant admin opens
   <https://outlook.office.com/connectors/oam/admin> in the customer tenant.
2. **Consent 3P Apps** → find your service's Entra app → **Approve** →
   **Consent on behalf of your organization**.

Once approved, the provider is active in that tenant and Outlook will render
inline action buttons. Without consent, customers fall back to Mode B (HTML).

---

## 8. The Teams hand-off (works in both modes)

The "Review with Assistant" button in either mode is an HTTPS link of the form

```
https://<service-base>/cta/handoff?token=<signed-jwt>&surface=teams
```

The token carries the email's context (batch id, exception ids, persona, etc.).
The service:

1. Validates the token.
2. Looks up the user's stored Teams conversation reference (captured when the
   user first messaged the bot, or via `conversationUpdate` when the bot was
   installed).
3. Pushes a context-laden Adaptive Card into that conversation
   (proactive message).
4. Redirects the browser to the Teams deep link for that chat.

The user lands in Teams with the card already at the top — same context
regardless of which email mode they came from.

If the bot isn't installed in the customer's Teams yet, the service shows a
small interstitial page explaining how to add it (one-time per customer).

---

## 9. Validation checklist

Before declaring "done", verify:

- [ ] A test email arrives in a mailbox listed under the provider's Test Users.
- [ ] Opening the email in Outlook desktop renders the Adaptive Card inline.
- [ ] Per-row action buttons (`Action.Http`) are visible.
- [ ] Clicking an action button shows a status banner and refreshes the card
      body within ~1 second; no browser tab opens.
- [ ] Opening the same email in Outlook mobile shows the HTML fallback with a
      working "Review with Assistant" CTA.
- [ ] Clicking "Review with Assistant" opens the Teams chat and the assistant
      delivers the email's context within a few seconds.
- [ ] Service logs show the authenticated user's UPN/email on every action
      call (verifies token validation is wired up).

---

## 10. Troubleshooting

| Symptom                                                   | Likely cause                                                                |
| --------------------------------------------------------- | --------------------------------------------------------------------------- |
| Card doesn't render at all (HTML body shown instead)       | Tenant strips `<script>` block (Defender / mail flow rule); or sender not on the provider's allow list. |
| Card renders, action buttons missing                       | Card's `originator` GUID doesn't match a registered, consented provider; or the Entra app isn't pre-authorized for the Actions client. |
| Card renders, buttons present, click silently no-ops       | Customer tenant hasn't run "Consent 3P Apps" for the Entra app; or the action URL isn't under a registered Target URL. |
| Click returns red banner: `jwt invalid: audience`          | Service validates against wrong `aud` value — must match Entra App ID Uri (v1 tokens) or App Id GUID (v2 tokens). |
| Click returns red banner: `jwt invalid: signature`         | JWKS cache stale; or token issued for a tenant whose JWKS the service didn't fetch. |
| Teams hand-off opens chat but no card appears              | The service has no stored conversation reference for the user — bot needs to be installed, or the user needs to send any message once. |

---

## 11. Reference URLs

- Actionable Email Developer Dashboard: <https://outlook.office.com/connectors/oam/publish>
- Tenant Admin Consent Dashboard: <https://outlook.office.com/connectors/oam/admin>
- Microsoft documentation:
  - [Get started with actionable messages](https://learn.microsoft.com/en-us/outlook/actionable-messages/get-started)
  - [Enable Entra ID token authentication](https://learn.microsoft.com/en-us/outlook/actionable-messages/enable-entra-token-for-actionable-messages)
  - [Security requirements](https://learn.microsoft.com/en-us/outlook/actionable-messages/security-requirements)
- Pre-authorized client ID for the Actions service: `48af08dc-f6d2-435f-b2a7-069abd99c086`
