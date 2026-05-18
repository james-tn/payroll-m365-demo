# PayCycle — Payroll-in-M365 Demo

A working, end-to-end demo of a **payroll-processor ISV agent** distributed in
the Microsoft 365 ecosystem. Designed to illustrate the architecture options
discussed in customer engagements where the question is:

> *"What is the right way to bring payroll workflows into the M365 flow of work
> — Outlook, Microsoft Teams, and M365 Copilot — so users don't have to come
> back to our web app to take action?"*

The product, employees, and pay batches are entirely fictitious. Branding =
**"PayCycle"** by **Acme Manufacturing** (a fictional customer).

---

## What it demonstrates

### Scenario

Maria is a payroll admin at Acme Manufacturing. Her company runs **PayCycle**
as their payroll processor. During the May-B pay cycle:

- Joseph Smith logged 14h of overtime — **3.4× his trailing average**.
- Sarah Lee has 12 PTO hours from the previous cycle that were never approved.

PayCycle's backend detects both anomalies. From here:

1. **Maria gets an Outlook email** with an Adaptive Card listing the two
   exceptions. One button: *"💬 Discuss with PayCycle in Teams"*.
2. She clicks. She is redirected into Teams; a follow-up Adaptive Card is
   already waiting from the PayCycle bot with the same context. She talks
   to a LangGraph agent that uses semantic tools over the mock backend
   ("show me the variance", "approve overtime", "is the PTO already in a
   trailing batch?"), resolves both exceptions, and submits the batch.
3. PayCycle now emails **David** (the manager) an Adaptive Card with **two
   first-class buttons inside the email itself**: ✅ **Approve** (happy path
   — one click, no app switch) and ❌ **Reject with reason** (`Action.ShowCard`
   in-email form). A third button, *"💬 Get explanation in Teams"*, hands
   off to Teams with the batch summary preloaded.
4. Approval routes back through PayCycle, the batch moves to *approved*, and
   the email refreshes inline via `CARD-UPDATE-IN-BODY`.

The same agent and the same email-→-Teams handoff flow work from **Microsoft
365 Copilot** too — the agent is published as a Custom Engine Agent so it
appears alongside the user's other agents.

### Architecture patterns implemented

| Pattern | Where it shows up |
|---|---|
| **Outlook actionable email** with `Action.Http` + `Action.ShowCard` | Manager approval card |
| **Email → Teams handoff** (Pattern B) — `Action.OpenUrl` → backend `cta/handoff` → proactive push to existing Teams conversation → 302 to `teams.microsoft.com/l/chat` deep link | Both admin and manager cards |
| **Proactive messaging** via the stored `ConversationReference` | `src/bot/proactive.py` |
| **M365 Copilot Custom Engine Agent** distribution via the same app manifest | `manifests/m365/manifest.json` |
| **In-email inline card refresh** via the `CARD-UPDATE-IN-BODY` response header | `src/app.py:_outlook_card_response()` |
| **Action.Execute** invoke handlers (refresh card inside Teams) | `src/app.py:_handle_invoke()` |
| **Multi-tenant bot** (ISV pattern) — the bot's Entra app is multi-tenant, JWT issuer is not pinned | `src/app.py` JWT validation |
| **LangGraph React agent** with persona-scoped system prompts and tool-calling | `src/agent/graph.py` |
| **Single-use signed JWT** on every CTA URL with replay protection | `src/common/tokens.py` |

---

## Quick demo run

> Skip to **Sideload the app** below if you only want to click through
> the end-to-end flow.

**Live URL:** <https://payroll-m365-demo.politeground-c0ea36c5.eastus2.azurecontainerapps.io>

**Demo console (no auth, internal):**
<https://payroll-m365-demo.politeground-c0ea36c5.eastus2.azurecontainerapps.io/demo/console>

From the console you can:

1. **Send admin exception alert** → emails `janguy@microsoft.com` (Maria).
2. **Submit batch as Maria → email David** for approval.
3. **Reset state** to start the demo over.

> ⚠ Buttons inside the Outlook email will be **disabled** until you complete
> a one-time OAM registration. See [`docs/oam-registration.md`](docs/oam-registration.md).
> The Teams handoff flow works either way (button is `Action.OpenUrl`,
> which OAM does not gate).

---

## Sideload the app

1. Build the package:

   ```bash
   cd manifests/m365 && zip -j payroll-demo-app.zip manifest.json color.png outline.png
   ```

   (already shipped at `manifests/m365/payroll-demo-app.zip`)

2. In Teams → Apps → *Manage your apps* → *Upload an app* → *Upload a custom
   app* → select the zip. Pin it to chat.

3. In Microsoft 365 Copilot → *Agents* → *Add agent* → *Custom* → upload
   the same zip. The agent will appear in the side rail.

4. Say *hi* to the agent once in Teams **before** triggering the first email —
   this is how the bot captures your `ConversationReference` so that subsequent
   proactive pushes have somewhere to go. (This step would be replaced by a
   formal sign-in / subscription flow in production.)

---

## Local dev

```bash
git clone https://github.com/james-tn/payroll-m365-demo
cd payroll-m365-demo
uv venv --python 3.12
uv pip install -e .
cp .env.example .env  # fill in values
uv run uvicorn src.app:app --reload --port 8080
```

Run the smoke tests:

```bash
uv run --with pytest pytest tests/ -v
```

Tunnel to a public URL for bot testing:

```bash
devtunnel host -p 8080 --allow-anonymous   # or ngrok http 8080
# update the Bot endpoint to https://<tunnel>/api/messages
```

---

## Redeploy

```bash
infrastructure/deploy.sh
```

Rebuilds the image with ACR Tasks (no local Docker needed) and rolls a new
revision on the Container App.

---

## Azure resources

All in subscription `840b5c5c-3f4a-459a-94fc-6bad2a969f9d`,
resource group `payroll-m365-demo-rg`, region `eastus2`.

| Kind | Name |
|---|---|
| Azure Communication Services | `paycycle-acs-31210` |
| ACS Email service | `paycycle-email-svc` |
| Email sender mailbox | `DoNotReply@8cd3731a-c37e-4ac9-88d0-876dbcf5c3de.azurecomm.net` |
| Container Registry | `payrollm365demo1928` |
| Container Apps environment | `payroll-cae` |
| Container App | `payroll-m365-demo` |
| Bot service | `payroll-m365-demo-bot` |
| Bot Entra app (multi-tenant) | `8412d807-a2f1-4890-b106-a500c67e92a5` |
| Azure OpenAI | `eastus2oai` (deployment: `gpt-5.2-chat`) |

---

## Architecture diagram (logical)

```
              ┌──────────────────────────────────────────┐
              │   PayCycle backend (this repo, ACA)      │
              │                                          │
              │   FastAPI app                            │
   ┌──────────┤   ├── /api/messages  (Bot Framework)    │
   │          │   ├── /cta/approve   (Outlook Action.Http)
   │          │   ├── /cta/reject    (Outlook Action.ShowCard.Http)
   │          │   ├── /cta/handoff   (Outlook → Teams handoff)
   │          │   └── /demo/console  (operator UI)      │
   │          │                                          │
   │          │   LangGraph React agent (Azure OpenAI)  │
   │          │   FlexStore in-memory (mock backend)    │
   │          └─────────────────────────────────────────┘
   │                          │                ▲
   │                          │                │ proactive push
   │   email                  │ proactive      │ via stored
   │   (ACS)                  │ thread create  │ ConversationReference
   │                          ▼                │
   │   ┌──────────┐    ┌──────────┐    ┌──────────────┐
   └──►│ Outlook  │    │ M365     │    │  Microsoft   │
       │ inbox    │◄──►│ Copilot  │◄──►│  Teams       │
       │          │    │ side rail│    │  bot chat    │
       └──────────┘    └──────────┘    └──────────────┘
                          ▲                ▲
                          │                │
                          └── user: janguy@microsoft.com
                              (plays both Maria and David)
```

---

## Why two-stage email + chat handoff?

For a **payroll-processor ISV** working in someone else's tenant, every
notification surface has different trade-offs:

| Channel | Strength | Limit |
|---|---|---|
| Outlook actionable email | Lives where users already work; survives multi-day workflows; multi-tenant by default | Action set is fixed at send time; only `Action.Http` / `OpenUrl` / `ShowCard`; no long-running interactive dialogue |
| Teams / Copilot agent chat | Rich back-and-forth; long-running messages; refresh cards; agent reasoning | Requires the user to have *touched* the bot once so a `ConversationReference` exists; needs the app to be installed in their tenant |

The pattern this demo lands on:

1. **Initial notification = email.** Users *will* see it; works across tenants
   with zero setup; survives the user being away for days.
2. **Happy-path action = inline email button.** One-click `Action.Http`, refreshed
   inline with `CARD-UPDATE-IN-BODY`. No app switch.
3. **Anything that needs reasoning = handoff to Teams/Copilot.** Button is
   `Action.OpenUrl` → backend pushes the next card proactively → 302 to the
   Teams deep link. The same conversation reference works for the Copilot
   side rail.

The customer doesn't have to choose Outlook *or* Teams *or* Copilot up
front — they choose the **right surface per step of the workflow** and the
agent is the same agent across all three.

---

## Security notes (this is a demo)

- **Bot Entra app secret** and **ACS connection string** are stored as
  Container App secrets, not as plain env vars.
- **Token signing key** is generated at deploy time, also a Container App
  secret. CTA URLs carry a 60-minute single-use JWT (`jti` is tracked in-memory).
- **Multi-tenant JWT validation** in `src/app.py` does *not* pin issuer
  (`verify_iss=False`) so users from any tenant can authenticate against a bot
  whose Entra app lives in our tenant. Audience is validated against the bot
  app id. For production you would either (a) pin a tenant allow-list or
  (b) switch the Bot Service to `UserAssignedMSI` for first-class federated
  multi-tenant.
- The `/demo/console` operator UI has **no authentication** — fine for a
  short-lived public demo, **not** acceptable for any production-adjacent
  deployment.
- The mock FlexStore is **process-local in-memory**; restart the container
  and state resets.

---

## File layout

```
src/
  app.py                       # FastAPI: bot handler + CTA endpoints
  agent/graph.py               # LangGraph React agent + 6 tools
  bot/
    conversation_store.py      # in-memory ConversationReference cache
    proactive.py               # bot app token + push to stored conv
  cards/builders.py            # Adaptive Card JSON generators
  common/
    config.py                  # Pydantic Settings
    logging.py
    tokens.py                  # signed single-use JWT helpers
  demo_console/routes.py       # /demo/console operator UI
  email_service/sender.py      # ACS Email wrapper for actionable cards
  flex/store.py                # mock PayCycle backend + state machine
mock_data/
  company.json
  employees.json
  exceptions.json
  users.json
manifests/m365/
  manifest.json                # Teams + Copilot custom engine agent
  color.png  outline.png
  payroll-demo-app.zip         # sideload package
infrastructure/
  deploy.sh                    # rebuild + roll Container App revision
docs/
  oam-registration.md          # one-time OAM provider registration
tests/
  test_smoke.py                # store + cards + tokens
Dockerfile
pyproject.toml
.env.example
```
