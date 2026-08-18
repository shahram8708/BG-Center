# BG Command Centre - Step 1: Foundation

Enterprise platform managing the complete lifecycle of Bank Guarantees (BGs).
This is **Step 1** of an 8-step staged build and delivers the full foundation:
architecture, database schema (all 20 tables), authentication & authorization,
base UI shell/design system, PWA foundation, notification infrastructure, and
an admin-creation CLI.

## Quick start

```bash
# 1. Install dependencies (unversioned packages, see requirements.txt)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure the environment
cp .env.example .env
# ... edit .env (at minimum SECRET_KEY; SMTP is optional - email is logged
# to the console when SMTP_HOST is left blank)

# 3. Run the app (creates the SQLite database + all tables on first run)
python run.py            # development server (use_reloader=False)
# or
gunicorn wsgi:app        # production WSGI (via Gunicorn)
```

A fresh clone runs end-to-end with no manual database setup - the schema is
created automatically on first run, and `flask db upgrade` applies the Alembic
migrations for ongoing schema evolution.

## Getting a usable admin account

```bash
export FLASK_APP=app

# Create the single platform administrator. If ADMIN_PASSWORD is unset it
# generates and prints a random password once. Safe to run more than once
# (idempotent) and never creates any other users.
flask create-admin admin@bg.center
```

Additional users are managed through the in-browser User & Role Management page
(approving registrations, granting roles, and assigning business-unit scope).

## Step 2 - BG Intake & AI Validation Engine

Three new screens (sidebar → My Tasks):
- **Upload BG** (`/bg-upload`, creator role) - the full new-BG intake wizard.
- **Upload Extended BG** (`/bg-upload-extended`, coordinator role) - files an extension against a live parent BG.
- **Saved Drafts** (`/documents/drafts`, creator/coordinator) - resume a validated upload without re-running AI.

The intake pipeline is orchestrated as a Celery chain/chord with per-stage
`celery_jobs` tracking: `bg_extraction` (Stage 1: extraction + format checklist
in one multimodal Gemini call) → parallel `po_sap_cross_check` + `template_compliance`
→ `finalize_validation`. The front end polls `/api/pipeline/status/<bg_id>` and
offers stage-scoped retry via `/api/pipeline/retry/<bg_id>/<stage>`.

Setup for AI/SAP (see `.env.example`):
- `GEMINI_API_KEY` / `GEMINI_MODEL` - required for real Gemini calls. Without a
  key the pipeline surfaces a manual-retry state rather than running.
- `SAP_PO_ENDPOINT` (+ `SAP_CLIENT_ID`/`SAP_CLIENT_SECRET`/`SAP_BASE_URL`) - real
  SAP financial integration. When unset, the local fallback dataset
  (`sap_po_records`) is used.

The deterministic prohibited-clause engine (`services/prohibited_clauses.py`) is
pure Python: any rule match forces `effective_tier` to `prohibited` regardless of
the AI proposal; otherwise the AI tier stands. The ABG shortfall guardrail is a
hard, non-overridable block. Uploaded PDFs are stored under `uploads/` (never
web-servable) and streamed only through the authenticated, ownership-checked
route `/documents/<id>`.

## Step 3 - Multi-Stage Approval Workflow & BG Records

Six new screens plus the DoA engine:
- **Multi-Stage Approval Queue** (`/bg-multi-stage-approval`) - role-scoped queue
  with filters and saved views (Buyer / TC Head / BU FC / BU CFMC / ABEX).
- **BG Review Workspace** (`/bg-multi-stage-approval/<bg_id>`) - per-deviation
  Accept/Reject, the unconditional Prohibited-tier hard block, a TC-Head-only
  tier editor (with the deterministic severity floor), and whole-BG Reject.
- **CEO/CFO Mail Attachments** (`/bg-ceo-cfo-mail`, creator) - attach offline
  CEO/CFO email evidence to advance a BG past `pending_ceo_cfo`.
- **BG Detail & Timeline** (`/bg/<bg_id>`) - read-only canonical record.
- **Document Viewer** - `/documents/<id>` (chrome) + `/documents/<id>/raw`, access
  extended to every entitled role, not just the uploader.
- **Generated Documents** (`/documents/generated`) - versioned Comparison Report
  archive (deterministic PDF via ReportLab, no AI call).

The DoA matrix is fully data-driven from `application_settings.doa_matrix`
(stage sequences per expenditure type, the conditional CEO/CFO stage triggered
by High/Prohibited risk, and per-role deviation-tier visibility - default
permissive). The `application_settings` key holds the real structured matrix. Stage
transitions run synchronously; notification fan-out is dispatched through the
Celery task `workflow.notify_stage_transition` so delivery never blocks the
approver. A prohibited-tier deviation permanently disables Approve &amp; Forward
for every role (only an admin override in Step 7 can clear it); whole-BG Reject
is terminal and the record stays visible read-only on BG Detail.

## Step 4 - Extension, Closure & Return Lifecycle

Four new screens (sidebar → Lifecycle Hub, role-gated) plus the ABEX-only
Closure Verifications view on the approval queue:
- **BG Extension Management** (`/bg-extension`, coordinator) - sectioned by
  urgency; initiate vendor requests and pre-link into the Upload Extended BG page.
- **BG Closure Management** (`/bg-closure`, coordinator) - live-computed
  eligibility (standard vs exception, never user-chosen), exception justification,
  and tracking of the magic-link sign-off stages.
- **Closure Review** (`/bg-closure-category-lead`, tc_head) - review exception closures.
- **BG Return** (`/bg-return`, coordinator + limited creator) - requested →
  dispatched → receipt-confirmed using the shared courier/CMR form.

Scheduled (Celery Beat) tasks in `maintenance_tasks.py`:
- `maintenance.daily_expiry_scan` - creates `extension_requests` for Live BGs
  crossing the warning threshold and flags overdue items (thresholds from
  `application_settings.extension_policy`).
- `maintenance.daily_extension_digest` - one digest email per Coordinator.

The closure exception chain runs: TC Head review → sequential CFO-then-CEO
magic-link approval → ABEX verification. The **generic magic-link service**
(`services/magic_link_service.py`) is reusable - parameterized by record and
token/timestamp columns - so Step 5 reuses it for `bg_invocations` unchanged.
Tokens are cryptographically random, signed, time-limited, single-use, and
stored only as SHA-256 hashes. The CFO-then-CEO gate is enforced server-side
(the CEO endpoint rejects unless `cfo_approved_at` is set), and the ABEX
segregation-of-duties rule (cannot verify a closure you initiated or reviewed)
is enforced with no exception for multi-role users. An offline CFO/CEO evidence
attachment path is also supported. The `sap_service` PO/contract execution
check powers the eligibility engine via the same real-endpoint-or-local-fallback
pattern; `bg_closures` gained four columns (`cfo_approval_token`,
`cfo_approved_at`, `ceo_approval_token`, `ceo_approved_at`) via migration.

## Step 5 - BG Invocation & Legal Letter Generation

One page - **BG Invocation** (`/bg-invocation`, BU FC primary / TC Head
hold-support; sidebar → Lifecycle Hub). Sections:
- **Claim Window Monitor** - BGs at `approaching_window`/`critical`, with
  full-width danger alerts for critical items.
- **In Progress** - draft → signed → CEO-approved → on-hold, with an explicit
  dual-gate status display and sign-upload (confirmation-checkbox + PDF/10MB)
  for BU FC, and hold/release actions for TC Head.
- **Completed** - invocations `sent_to_bank` (read-only).

Scheduled task `maintenance.daily_claim_window_scan` (Celery Beat, alongside the
extension scan) creates/advances `bg_invocations` rows using
`invocation_policy.approaching_days`/`critical_days`.

Draft generation (`invocation.generate_draft`, job-tracked) asks Gemini for the
letter's variable content only, converts the amount to words deterministically
(`utils/numbers.py`, never AI), and merges it into a versioned starter DOCX
template (`bgcc/assets/invocation_letter_template.docx`) via docxtpl - producing
both a `.docx` (authoritative) and a `.pdf` (ReportLab) `generated_documents`
row sharing one version. It then dispatches the main CEO approval email via
Step 4's generic magic-link service, received through the same unmodified
`/executive-approval/<token>` route.

The **dual-gate send evaluator** (`invocation_service.evaluate_and_send`) is
idempotent and race-safe: it only sends when the signed letter is present, the
CEO has approved, and the invocation is not on hold, and it guards against
double-send - updating `bank_guarantees.status = submitted_to_bank` on send and
emailing the issuing bank (from `approved_banks.contact_email`) plus internal
notifications. There is no manual "Send to Bank" button.

The **hold workflow** immediately sets `stage = on_hold` (closing the race
window), then runs a sequential CFO-then-CEO magic-link chain (the CEO hold
email is only dispatched after CFO approval; the CEO gate rejects otherwise),
and re-derives the post-release stage from the actual sub-state. Tokens remain
hashed at rest, single-use, and time-limited. `bg_invocations` gained two hold
token columns (`hold_cfo_approval_token`, `hold_ceo_approval_token`) via
migration so the hold chain reuses the same generic service.

## CLI reference

| Command | Purpose |
|---|---|
| `flask create-admin [email]` | Create or promote an account to `admin`. Creates a single admin user only. |

Migrations:
```bash
flask db upgrade      # apply migrations
flask db migrate -m "message"   # generate a new migration
```

## Configuration

Every variable the application reads is documented in `.env.example` with
inline comments. Highlights:

- `FLASK_CONFIG` - `development` / `testing` / `production`.
- `SECRET_KEY` - session signing key (generate a strong random value).
- `DATABASE_URL` - SQLAlchemy URI (SQLite now; Postgres via config change later).
- `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` - Redis in production; eager/in-memory in dev.
- `RATE_LIMIT_ENABLED` - production-only rate limiting toggle.
- `SMTP_*` - transactional email; when `SMTP_HOST` is blank, email content is
  logged to the console (safe local fallback) instead of failing.
- `COMPANY_EMAIL_DOMAIN` - the work-email domain enforced at registration.

## Celery (background jobs)

All notification sends are routed through a Celery task (`notification.send`)
with real `celery_jobs` tracking. In development the task runs eagerly; in
production run a worker with:

```bash
celery -A celery_app:celery worker --loglevel=info
```

## Security

- Passwords hashed with a strong, salted algorithm; complexity policy enforced.
- Secure, HTTP-only, same-site session cookies (secure flag on in production).
- CSRF enabled globally on every form (never disabled).
- Production-only rate limits + a short per-email account lockout.
- Every auth event writes a structured `audit_log` row (IP, actor, target, metadata).
- Role-based access enforced server-side against the session's `active_role`
  via `roles_required` / `admin_required`; multi-role active-role switching.
- No raw SQL; template auto-escaping on; no secrets committed.

## Project layout

```
app.py / run.py / wsgi.py / celery_app.py   entry points (use_reloader=False)
requirements.txt                            unversioned package names only
.env.example                                documented environment variables
migrations/                                 Flask-Migrate (Alembic)
uploads/ generated/                         file storage (empty, gitignored)
bgcc/
  __init__.py      application factory
  config.py        Development/Testing/Production config classes
  extensions.py    db, migrate, login_manager, csrf, limiter
  content.py       reusable constants (standing AI disclaimer)
  cli.py           create-admin
  models/          all 20 tables + shared enums
  routes/          auth, dashboard, legal + empty blueprints for Steps 2–8
  services/        audit, notification, security/access + future service stubs
  tasks/           notification task + future task stubs
  forms/           sign-in, register, forgot/reset, role-select
  utils/           files, security tokens, validators, logging
  templates/       base, layouts, components, macros, per-blueprint pages
  static/          css, js, vendor (Bootstrap), icons, manifest, service worker
```

## Step 6 - Reporting, Portfolio Dashboard, Q&A Assistant & Account Management

Seven screens:
- **Dashboard** (`/dashboard`) - real, cache-backed portfolio analytics (KPI cards
  for active and bank-confirmed value, value/count-toggle bar charts by bank,
  vendor and business unit, and a BG-type mix donut via Chart.js). Aggregates are
  pre-computed hourly by `maintenance.warm_dashboard_cache` and stored in
  Flask-Caching; the route only reads the cache. Admins see the company-wide
  entry, others their own SAP system. Fresh instances warm the cache on first run.
- **BG Status Hub** (`/bg-status`) - company-wide, server-side filtered/searched/
  paginated list (status, type, expenditure, business unit, vendor, date range)
  with saved views, linking into BG Detail.
- **Bank Tracker** (`/bg-bank-tracker`) - bank-side authenticity verification,
  automatically triggered when a BG goes Live (additive hook in ABEX Verify),
  via Step 4's generic magic-link service (third consumer, unchanged). A 30-minute
  poll expires unanswered tokens to `no_response`; Coordinators can resend or
  apply a manual confirm/dispute override with a reference note.
- **Policy Q&A Assistant** (`/assistant/`) + a global persistent widget - TF-IDF
  retrieval over `policy_reference_content` + live configuration, then a
  synchronous Gemini call (the documented exception), source citations, deep
  links, bilingual (English/Hindi), graceful decline when context is insufficient,
  and per-user persisted history in `assistant_messages`.
- **Notifications Center** (`/notifications/`) - filterable, paginated history
  with per-item and bulk mark-read.
- **Profile & Preferences** (`/profile/`) - tabbed Account / Roles / Preferences /
  Notifications, reading/writing `user_preferences`.

Two new tables via migration: `bank_verifications` and `assistant_messages`.
New `application_settings` keys: `bank_verification_expiry_hours` and
`policy_reference_content` (configuration). Celery Beat gained two jobs
(`hourly-dashboard-warm`, `bank-verification-poll`). `requirements.txt` stays
unversioned (added `flask-caching`, `scikit-learn`); Chart.js is loaded via CDN.

## Step 7 - Admin Suite & Platform Configuration

Four admin-only screens (sidebar → Admin), each closed out from deferred work in
earlier steps:
- **Admin Dashboard** (`/admin/`) - KPI cards for pending registrations and
  prohibited-clause overrides, plus background-job health and recent failed jobs
  computed from `celery_jobs`.
- **User & Role Management** (`/admin/users`) - approve/reject (archive, not
  delete) pending registrations and edit any user's roles/business-unit scope.
- **Platform Configuration** (`/admin/configuration`) - structured, non-JSON
  editors for every `application_settings` key: DoA & approval rules (visibility
  grid + CEO/CFO trigger tier), clause template (repeatable clauses), prohibited
  patterns (regex-validated), checklist & banks, lifecycle policy (extension /
  invocation / executive contacts), policy-assistant content, and SAP business
  units. Every save requires a change reason, increments `version`, sets
  `changed_by`, and writes a `config_changed` audit row.
- **Audit Log Viewer** (`/admin/audit-log`) - server-side filtered/paginated
  view of `audit_log` with a working CSV export.

The **prohibited-clause admin override** (`/admin/prohibited-overrides`) is the
only path past the hard block on Prohibited-tier deviations: it records
`admin_override_by`/`admin_override_at`/`admin_override_reason` on the deviation
without ever changing `effective_tier`, requires a substantive justification and
an explicit confirmation, and additively extends the approval enablement check so
an overridden Prohibited deviation no longer blocks forwarding. All relevant
badges now read "Prohibited - Admin Override Granted." `deviations` gained the
three override columns via migration.

## Step 8 - Production Hardening, PWA Completion & Final QA

**PWA completion.** The service worker (`static/sw.js`) now differentiates
offline behavior by page category: fully-static pages (About, AI Disclaimer,
Privacy, Terms) are cached cache-first; read-only informational pages (Dashboard,
BG Status Hub, Bank Tracker, BG Detail, Notifications, Audit Log) use a
network-first, cache-fallback strategy with an honest "showing cached data"
banner; and every workflow-changing page/action is never cached for offline
submission - all POST forms are disabled offline with clear messaging, and
offline navigation falls back to a rewritten `/offline` page. A custom,
well-timed install prompt replaces the bare browser default (an Install button in
the navbar appears after genuine engagement).

**Push notifications.** Real browser push via VAPID keys (`VAPID_PUBLIC_KEY`,
`VAPID_PRIVATE_KEY`, `VAPID_CLAIMS_EMAIL` in `.env`). The `user_preferences`
table gained a `push_subscription` column via migration; the client requests
permission only after a contextual prompt (Profile → enable push), subscribes via
the Push API, and posts the subscription to `/api/push/subscribe`. The Step 1
push-channel stub was replaced with genuine web-push delivery at its exact
existing call sites.

**Rate limiting.** Verified wired end to end and active in production
(`RATE_LIMIT_ENABLED=true` in `.env`). Coverage across categories:
- Auth: sign-in 10/min, registration 10/hour, forgot-password 10/hour (per-IP)
  plus the independent per-email account lockout.
- Uploads: BG uploads 10/hour, CEO/CFO mail 20/hour, closures/returns 20/hour,
  signed invocation 20/hour.
- Public magic-link verification (`/executive-approval/<token>`): 5/min (POST).
- Q&A assistant (synchronous Gemini): 30/hour per-user.
- Prohibited-clause override: 10/min.
- Approval decisions 60/hour, closure review 30/hour, closure verify 30/hour.

**Security & hardening.** Final audit confirmed: no raw SQL concatenation, no
hard-coded secrets, CSRF on every form/AJAX, server-side RBAC on every route,
document-serving ownership intact for all document types, all magic-link tokens
hashed/single-use/time-limited and never logged in plaintext, the prohibited-tier
severity floor and override path intact, and race-safe dual-gate/SoD controls.

**End-to-end walkthrough.** A 12-scenario lifecycle test was executed against the
running integrated application - onboarding, standard CAPEX/OPEX intake-to-Live,
elevated-risk via CEO/CFO mail, the prohibited-block + admin override unblock,
extension flag/request/link, standard + exception closure (magic-link chain +
segregation-of-duties), return, invocation dual-gate auto-send, bank verification,
bilingual grounded Q&A assistant, and admin config/audit export - all passing with
no cross-step integration gaps.

## Production run

```bash
export FLASK_CONFIG=production
export SECRET_KEY="$(python -c 'import secrets;print(secrets.token_hex(32))')"
export RATE_LIMIT_ENABLED=true
export SESSION_COOKIE_SECURE=true
export CELERY_BROKER_URL=redis://localhost:6379/0
export CELERY_RESULT_BACKEND=redis://localhost:6379/1
flask db upgrade
gunicorn -w 4 -b 0.0.0.0:8000 wsgi:app
celery -A celery_app:celery worker --loglevel=info
celery -A celery_app:celery beat --loglevel=info
```
