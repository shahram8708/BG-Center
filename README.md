# 🏦 BG Command Centre

> **Enterprise platform that manages the complete lifecycle of Bank Guarantees - from intake, AI validation and multi-stage approval, all the way to extension, closure, invocation and return.**

[![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-Framework-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Celery](https://img.shields.io/badge/Celery-Background%20Tasks-37814A?logo=celery&logoColor=white)](https://docs.celeryq.dev/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-ORM-D71F00)](https://www.sqlalchemy.org/)
[![License](https://img.shields.io/badge/license-Internal%20Use-lightgrey)](#license)
[![PWA](https://img.shields.io/badge/PWA-Installable-5A0FC8?logo=pwa&logoColor=white)](https://web.dev/progressive-web-apps/)
[![Status](https://img.shields.io/badge/Status-8%20of%208%20steps%20complete-success)](#about-the-project)
[![Repo](https://img.shields.io/badge/GitHub-shahram8708%2FBG--Center-181717?logo=github)](https://github.com/shahram8708/BG-Center)

---

## 📑 Table of Contents

- [About the Project](#about-the-project)
- [Key Features](#key-features)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
  - [Environment Variables](#environment-variables)
  - [Running the Project](#running-the-project)
- [Usage](#usage)
- [API & Route Map](#api--route-map)
- [Configuration](#configuration)
- [Testing](#testing)
- [Deployment](#deployment)
- [Contributing](#contributing)
- [Roadmap](#roadmap)
- [License](#license)
- [Acknowledgements](#acknowledgements)
- [Contact](#contact)

---

## 🏛️ About the Project

**Bank Guarantees (BGs)** are how trust is encoded in commerce - a bank says "if this counterparty fails, we'll pay." Managing them inside a large organization is brutal: PDFs flying around in email, clauses getting silently weakened, extension dates that slip past, exceptions that bypass finance, and a hard-to-reconstruct audit trail when something goes wrong.

**BG Command Centre** is the platform we built to fix that. It takes a Bank Guarantee from the moment a creator uploads the PDF to the moment the issuing bank confirms the closure receipt - and every meaningful action in between is captured, routed, and auditable.

The platform is meant for **treasury, finance, and procurement teams** at mid-to-large enterprises that issue hundreds or thousands of BGs a year and need a single source of truth for the entire lifecycle.

What makes it different:
- **A deterministic prohibited-clause engine** that can *never* be downgraded by an approver, only by an admin override
- **A reusable magic-link service** that powers closure sign-off, invocation hold/release, and bank verification from one set of code
- **A data-driven DoA matrix** that admins can edit without a code deploy
- **Race-safe dual-gate logic** so a BG can never be accidentally sent to the bank twice

---

## ✨ Key Features

- **📄 AI-powered intake** - Upload a BG PDF, Gemini extracts fields, checks format against the active clause template, cross-references SAP PO records, and produces a risk-tiered deviation report - all orchestrated as a Celery pipeline with per-stage retry.
- **🔁 Multi-stage approval workflow** - Data-driven Delegation-of-Authority (DoA) matrix. Different stage sequences for CAPEX vs OPEX. High/Prohibited-risk BGs route through an extra elevated CEO/CFO stage. Every transition is logged.
- **🛡️ Prohibited-clause hard block** - Any match against the prohibited-patterns list forces `effective_tier = prohibited` and disables "Approve & Forward" for every role. Only an admin can clear it (with a mandatory justification).
- **🔐 Generic magic-link service** - Single reusable service for executive approvals, invocation holds, and bank verification. Tokens are cryptographically random, signed, time-limited, single-use, and stored as SHA-256 hashes - never plaintext.
- **⚖️ Race-safe dual-gate send** - Bank send to the issuing bank only happens when both the signed letter is uploaded *and* the CEO has approved, with idempotent guards against double-send.
- **📊 Cached portfolio dashboard** - Aggregates are pre-computed hourly by a Celery Beat task and stored in Flask-Caching. The dashboard route never runs a heavy query on a request thread.
- **🌐 Bilingual Q&A assistant** - TF-IDF retrieval over policy docs + live config, then a synchronous Gemini call with source citations. English + Hindi. Gracefully declines when context is insufficient.
- **📲 Real Progressive Web App** - Service worker with category-specific offline behavior, install prompt, and VAPID-signed push notifications via the native Push API.
- **🛡️ Defense in depth** - Per-email account lockout + per-IP rate limits, CSRF on every form, hashed passwords, secure HTTP-only SameSite cookies, no raw SQL, no plaintext tokens in logs, structured audit log on every meaningful event.
- **🧑‍💼 Admin suite** - User/role management, structured editors for every `application_settings` key, prohibited-clause overrides, audit log viewer with CSV export.
- **📑 Comparison reports** - Deterministic PDF (ReportLab) versioned archive for every BG. No AI call - predictable, reproducible.
- **🔌 Pluggable SAP integration** - When `SAP_PO_ENDPOINT` is set, it calls a real SAP OAuth endpoint. When not, it falls back to a clearly-labeled local `sap_po_records` dataset. No mocking drama.

---

## 🧰 Tech Stack

### Backend
| Tool | Purpose |
|---|---|
| **Python 3.x** | Core language |
| **Flask** | Web framework, with the application-factory pattern |
| **Flask-SQLAlchemy** | ORM over 20 relational tables |
| **Flask-Migrate** | Alembic-based schema migrations |
| **Flask-Login** | Session-based authentication |
| **Flask-WTF / Flask-Limiter** | CSRF protection and rate limiting |
| **Flask-Caching** | In-memory cache for dashboard aggregates |
| **Celery** | Background task queue (intake pipeline, notifications, scheduled scans) |
| **Redis** (prod) / in-memory (dev) | Celery broker + result backend |
| **Gunicorn** | Production WSGI server |

### AI / Document Generation
| Tool | Purpose |
|---|---|
| **google-genai** | Official Google Gemini SDK (multimodal PDF + structured JSON output) |
| **ReportLab** | Deterministic PDF generation (Comparison Reports, Invocation Letters) |
| **python-docx** + **docxtpl** | Authoritative DOCX generation for invocation letters |
| **scikit-learn** | TF-IDF retrieval for the Q&A policy assistant |

### Database
- **SQLite** for development (zero-setup, file-based)
- **PostgreSQL** ready via `DATABASE_URL` (e.g. `postgresql+psycopg2://user:pass@host/db`)

### Frontend
- Server-rendered **Jinja2** templates
- **Bootstrap** (vendored under `bgcc/static/vendor/`)
- **Chart.js** (CDN) for the portfolio dashboard
- Vanilla JS for the pipeline progress poller, push subscription, and Q&A widget

### DevOps / Operations
- **Gunicorn** for production WSGI
- **Celery worker** + **Celery Beat** for scheduled tasks
- **pywebpush** for VAPID-signed browser push notifications
- **ProxyFix** middleware for correct URL generation behind a reverse proxy

### Integrations (Optional, via env)
- **Google Gemini API** - AI validation
- **SAP OAuth** endpoint - Real PO/contract cross-check (with a local `sap_po_records` fallback)
- **SMTP** for transactional email (with a console-log fallback when `SMTP_HOST` is blank)

### Other Utilities
- `itsdangerous` - Signed tokens
- `python-dotenv` - `.env` loading
- `email-validator` - Email validation
- `requests` - SAP HTTP calls
- `cryptography` (transitive via pywebpush) - VAPID key handling

---

## 🗂️ Project Structure

```
.
├── app.py                          # WSGI entry (create_app)
├── run.py                          # Dev server (host=0.0.0.0, port=5000, no reloader)
├── wsgi.py                         # Gunicorn entry
├── celery_app.py                   # Celery entry (initializes Flask + Celery)
├── requirements.txt                # Unversioned package list
├── .env.example                    # Every env var, documented inline
├── .gitignore                      # Standard Flask ignores
│
├── migrations/                     # Flask-Migrate (Alembic) - created on first flask db upgrade
│
├── uploads/                        # User-uploaded PDFs (gitignored, never web-servable)
├── generated/                      # Server-generated DOCX/PDF (gitignored)
│
└── bgcc/                           # The application package
    ├── __init__.py                 # create_app() factory - wires extensions, blueprints, CLI, errors
    ├── config.py                   # Config / DevelopmentConfig / TestingConfig / ProductionConfig
    ├── extensions.py               # db, migrate, login_manager, csrf, limiter, cache
    ├── celery.py                   # Celery instance + beat schedule (5 jobs)
    ├── cli.py                      # flask create-admin, users approve, seed-dev-data
    ├── content.py                  # Standing AI disclaimer constant
    │
    ├── models/                     # All 20 ORM tables + enums
    │   ├── enums.py                # PlatformRole, BGStatus, DeviationTier, WorkflowAction, ...
    │   ├── users.py                # User, UserPreference
    │   ├── reference.py            # SapSystem, BankGuarantee
    │   ├── documents.py            # Document, DocumentAnalysis
    │   ├── deviations.py           # Deviation (+ admin override columns)
    │   ├── generated_documents.py  # GeneratedDocument
    │   ├── workflow.py             # WorkflowHistory
    │   ├── dispatches.py           # Dispatch (courier / CMR)
    │   ├── lifecycle.py            # ExtensionRequest, BgClosure, BgReturn, BgInvocation
    │   ├── ai.py                   # AiInteraction (latency, tokens, errors)
    │   ├── jobs.py                 # CeleryJob (every task tracked)
    │   ├── notifications.py        # Notification (in-app inbox)
    │   ├── saved_views.py          # SavedView (per-user queue filters)
    │   ├── audit.py                # AuditLog (every meaningful event)
    │   ├── settings.py             # ApplicationSetting (versioned config)
    │   ├── sap_reference.py        # SapPoRecord (local PO fallback)
    │   ├── bank_verifications.py   # BankVerification (3rd-party magic-link consumer)
    │   └── assistant_messages.py   # AssistantMessage (Q&A history)
    │
    ├── routes/                     # 16 Flask blueprints
    │   ├── auth.py                 # Sign in / register / forgot / role-select
    │   ├── dashboard.py            # /dashboard (cached portfolio analytics)
    │   ├── legal.py                # Static legal pages
    │   ├── intake.py               # /bg-upload, /bg-upload-extended
    │   ├── approval.py             # /bg-multi-stage-approval queue + review
    │   ├── bg.py                   # /bg/<id> canonical record + report gen
    │   ├── lifecycle.py            # Extension / closure / return / executive-approval
    │   ├── invocation.py           # /bg-invocation (claim window + dual-gate)
    │   ├── hub.py                  # Lifecycle hub landing
    │   ├── documents.py            # /documents/<id> ownership-checked streaming
    │   ├── reports.py              # /bg-status, /bg-bank-tracker
    │   ├── assistant.py            # /assistant/ (Q&A) + global widget
    │   ├── profile.py              # Profile / preferences / push opt-in
    │   ├── notifications.py        # /notifications inbox
    │   ├── admin.py                # /admin/* (Step 7 suite)
    │   └── api.py                  # JSON API (pipeline status, push, parent search)
    │
    ├── services/                   # Business logic (testable, route-independent)
    │   ├── access.py               # roles_required / admin_required decorators
    │   ├── access_service.py       # can_view_bg() ownership + scope rules
    │   ├── audit_service.py        # record(event_type, ...)
    │   ├── notification_service.py # dispatch() - in-app + Celery + fallback SMTP
    │   ├── magic_link_service.py   # Generic signed-token service (3 consumers)
    │   ├── gemini_service.py       # Schema-constrained Gemini client + retries
    │   ├── prohibited_clauses.py   # Deterministic tier-flooring engine
    │   ├── sap_service.py          # Real SAP endpoint or local fallback
    │   ├── workflow_service.py     # DoA matrix + stage sequencing
    │   ├── analytics_service.py    # Dashboard aggregates + cache warm
    │   ├── assistant_service.py    # TF-IDF retrieval + bilingual Gemini
    │   ├── invocation_service.py   # Dual-gate send + hold chain
    │   ├── closure_service.py      # Eligibility, magic-link chain, SoD rules
    │   ├── extension_service.py    # Extension state machine
    │   ├── bank_verification_service.py # Authenticity checks
    │   ├── report_service.py       # Deterministic PDF Comparison Report
    │   ├── docx_service.py         # DOCX template rendering
    │   ├── seed_service.py         # Initial data + starter settings
    │   └── files.py                # Safe filename + display name
    │
    ├── tasks/                      # Celery tasks (registered in bgcc.celery)
    │   ├── notification_tasks.py   # notification.send
    │   ├── ai_tasks.py             # bg_extraction, po_sap_cross_check, template_compliance, finalize_validation
    │   ├── workflow_tasks.py       # notify_stage_transition, send_executive_approval_email
    │   ├── invocation_tasks.py     # generate_draft, evaluate_and_send
    │   ├── maintenance_tasks.py    # 5 Beat jobs: expiry/extension/claim scans, cache warm, bank poll
    │   └── document_tasks.py       # PDF comparison report (ReportLab)
    │
    ├── forms/                      # WTForms
    │   ├── auth_forms.py           # SignIn, Register, Forgot/Reset, RoleSelect
    │   └── intake_forms.py         # NewBgDetails, ExtendedBgDetails, IntakeReview
    │
    ├── utils/
    │   ├── urls.py                 # build_absolute_url, get_base_url (proxy-aware)
    │   ├── security_tokens.py      # Password reset tokens
    │   ├── numbers.py              # Number-to-words (used in invocation letter)
    │   ├── validators.py           # Custom form validators
    │   ├── files.py                # File handling helpers
    │   └── logging.py              # Structured event/error log + request_id
    │
    ├── templates/                  # Jinja2
    │   ├── layouts/                # base, sidebar, auth
    │   ├── components/             # Reusable partials
    │   ├── macros/                 # Form + UI macros
    │   ├── auth/                   # Sign in, register, forgot, reset, role-select
    │   ├── dashboard/              # Portfolio dashboard
    │   ├── intake/                 # Upload BG, extended BG, progress
    │   ├── approval/               # Queue, review workspace, closure verifications
    │   ├── bg/                     # BG detail with timeline
    │   ├── lifecycle/              # Extension, closure, return, executive approval
    │   ├── invocation/             # Claim window monitor, in progress, completed
    │   ├── hub/                    # Lifecycle hub
    │   ├── documents/              # Document viewer + generated archive
    │   ├── reports/                # BG status hub, bank tracker
    │   ├── assistant/              # Q&A full page
    │   ├── profile/                # Tabs: account, roles, preferences, notifications
    │   ├── admin/                  # Admin dashboard, users, config, overrides, audit log
    │   ├── legal/                  # AI disclaimer, privacy, terms, about
    │   ├── errors/                 # 403, 404, 413, 500
    │   └── emails/                 # account_approved, executive_approval, bank_verification, digest
    │
    ├── static/
    │   ├── css/                    # Custom styles
    │   ├── js/                     # Pipeline poller, push, Q&A widget, install prompt
    │   ├── icons/                  # PWA icons (192, 512, SVG)
    │   ├── vendor/                 # Bootstrap (vendored)
    │   ├── manifest.json           # PWA manifest
    │   └── sw.js                   # Service worker (category-aware offline)
    │
    └── assets/
        └── invocation_letter_template.docx  # Versioned DOCX starter for invocation letters
```

---

## 🚀 Getting Started

### Prerequisites

You'll need:

| Tool | Version | Notes |
|---|---|---|
| **Python** | 3.10+ | Tested on 3.x; uses modern type-hint syntax in places |
| **pip** | latest | For installing dependencies |
| **Git** | any | To clone the repo |
| **Redis** *(production only)* | 6+ | Required for the Celery broker in production; in dev, Celery runs in-memory |
| **A Gemini API key** *(optional)* | - | Needed for real AI validation; without it the pipeline surfaces a manual-retry state |

Install Python: <https://www.python.org/downloads/>
Install Redis (optional, for production): <https://redis.io/download>

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/shahram8708/BG-Center.git
cd BG-Center

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Linux / macOS
# .venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements.txt
```

### Environment Variables

Copy the example file and edit it:

```bash
cp .env.example .env
```

At minimum, you should set **`SECRET_KEY`** to a long random string. Generate one with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

| Variable | Description | Example |
|---|---|---|
| `FLASK_CONFIG` | Which config class to load | `development` / `testing` / `production` |
| `SECRET_KEY` | Flask session signing key | `change-me-to-a-long-random-string` |
| `DATABASE_URL` | SQLAlchemy URI | `sqlite:///bgcc_dev.db` (or `postgresql+psycopg2://...`) |
| `CELERY_BROKER_URL` | Celery broker | `redis://localhost:6379/0` (blank = eager/in-memory) |
| `CELERY_RESULT_BACKEND` | Celery result backend | `redis://localhost:6379/1` (blank = in-memory) |
| `RATE_LIMIT_ENABLED` | Enable per-IP rate limits | `false` in dev, `true` in prod |
| `SESSION_COOKIE_SECURE` | HTTPS-only session cookie | `false` in dev, `true` in prod |
| `SESSION_IDLE_HOURS` | Idle session timeout | `8` |
| `MAX_UPLOAD_MB` | Max upload size (MB) | `25` |
| `LOGIN_ATTEMPT_LIMIT` | Per-email failed-attempt threshold | `5` |
| `LOGIN_LOCKOUT_MINUTES` | Lockout duration after threshold | `15` |
| `SMTP_HOST` | SMTP server host | *(blank = log to console)* |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USER` / `SMTP_PASSWORD` | SMTP credentials | |
| `SMTP_FROM` | From address | `no-reply@bgcc.local` |
| `SMTP_USE_TLS` / `SMTP_USE_SSL` | TLS / SSL flags | `true` / `false` |
| `COMPANY_NAME` | Product display name | `BG Command Centre` |
| `COMPANY_EMAIL_DOMAIN` | Work-email domain enforced at registration | `bg.center` |
| `PWA_NAME` / `PWA_SHORT_NAME` | PWA manifest names | `BG Command Centre` / `BGCC` |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` / `ADMIN_NAME` | Default admin (if not generated) | |
| `DEFAULT_SAP_SYSTEM` | Default SAP code for new users | `GRP001` |
| `GEMINI_API_KEY` | Google GenAI API key *(Step 2)* | *(blank = no AI validation)* |
| `GEMINI_MODEL` | Gemini model name | `gemini-2.5-flash` |
| `SAP_PO_ENDPOINT` | Real SAP PO endpoint *(Step 2)* | *(blank = local `sap_po_records`)* |
| `SAP_CLIENT_ID` / `SAP_CLIENT_SECRET` / `SAP_BASE_URL` | SAP OAuth creds | |
| `NEW_BG_MAX_MB` | New BG upload size cap | `20` |
| `EXTENDED_BG_MAX_MB` | Extended BG upload size cap | `10` |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | Web-push keys | Generate with pywebpush (see below) |
| `VAPID_CLAIMS_EMAIL` | Web-push contact email | `admin@bg.center` |

Generate VAPID keys with:
```bash
python -c "from pywebpush import generate_vapid_keys; import json; print(json.dumps(generate_vapid_keys()))"
```

### Running the Project

#### Development

```bash
# Make sure your virtualenv is active and .env exists
python run.py
# → Serves on http://0.0.0.0:5000

# In a second terminal, create the platform administrator
export FLASK_APP=app
flask create-admin admin@bg.center
# → If ADMIN_PASSWORD is unset, prints a generated one to the terminal.
```

On first run, the app **automatically creates the SQLite database and all 20 tables**, seeds starter reference data (SAP systems, PO records, settings), and warms the dashboard cache. No manual `flask db upgrade` is required for a fresh clone.

#### Production

```bash
export FLASK_CONFIG=production
export SECRET_KEY="$(python -c 'import secrets;print(secrets.token_hex(32))')"
export RATE_LIMIT_ENABLED=true
export SESSION_COOKIE_SECURE=true
export CELERY_BROKER_URL=redis://localhost:6379/0
export CELERY_RESULT_BACKEND=redis://localhost:6379/1

flask db upgrade                          # Apply Alembic migrations
gunicorn -w 4 -b 0.0.0.0:8000 wsgi:app    # WSGI server
celery -A celery_app:celery worker --loglevel=info   # Background worker
celery -A celery_app:celery beat --loglevel=info     # Scheduled task scheduler
```

---

## 🧑‍💻 Usage

### End-to-end happy path

1. **Register** a user at `/auth/register` (must use the configured `COMPANY_EMAIL_DOMAIN`).
2. **Admin approves** the registration at `/admin/users`, assigning roles and a SAP business-unit scope.
3. **Creator logs in**, navigates to *Upload BG*, fills in the form, attaches a PDF, and submits. The intake pipeline kicks off:
   - `bg_extraction` (Gemini multimodal call) → classification + extracted fields
   - `po_sap_cross_check` + `template_compliance` (in parallel) → deviations with `low`/`high`/`prohibited` tiers
   - `finalize_validation` → flags ABG shortfall (hard block) and computes dispatch readiness
4. The frontend polls `/api/pipeline/status/<bg_id>` and shows live progress.
5. **Creator reviews** deviations, fixes what can be fixed, and submits for approval.
6. **Buyer → TC Head → BU FC/CFMC** (and elevated CEO/CFO if any high/prohibited tier exists) take their turns. Each one sees only the deviations their role is permitted to see (data-driven by `doa_matrix.deviation_visibility`).
7. **ABEX verifies** the BG, which moves it to `live`. Bank tracker polls begin automatically.
8. From here, the BG can be **extended** (pre-linked to Upload Extended BG), **closed** (with magic-link CFO→CEO sign-off for exception closures), **invoked** (claim window monitor + dual-gate auto-send to the bank), and **returned** (courier/CMR tracking).

### CLI example

```bash
# Approve a user from the command line
flask users approve jane.doe@bg.center --roles=buyer,tc_head --sap-system=GRP001

# Seed dev data
flask seed-dev-data

# Seed local PO records for the SAP fallback
flask seed-purchase-orders
```

### Migrations

```bash
flask db upgrade              # Apply migrations
flask db migrate -m "message" # Generate a new migration
```

---

## 🛣️ API & Route Map

The platform is mostly server-rendered, but exposes a small set of JSON endpoints for the pipeline poller, push subscriptions, and PO autocomplete.

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/` | Sign in (or redirect to dashboard if authenticated) | Public |
| `GET` / `POST` | `/auth/register` | Self-registration (must use `COMPANY_EMAIL_DOMAIN`) | Public, rate-limited |
| `GET` / `POST` | `/auth/forgot` / `/auth/reset` | Password reset flow | Public |
| `GET` | `/auth/role-select` | Multi-role users pick their active role | Login required |
| `GET` | `/dashboard` | Cached portfolio analytics | Login required |
| `GET` / `POST` | `/bg-upload` | New BG intake wizard (creator) | creator role |
| `GET` / `POST` | `/bg-upload-extended` | Extended BG intake (coordinator) | coordinator role |
| `GET` | `/bg-upload/<id>/progress` | Live progress page (polls API) | creator or coordinator (owner) |
| `GET` | `/documents/drafts` | Resume a validated upload | creator or coordinator |
| `GET` | `/bg-multi-stage-approval` | Role-scoped queue with filters + saved views | Queue role |
| `GET` | `/bg-multi-stage-approval/<bg_id>` | Per-deviation Accept/Reject workspace | Authorized role for stage |
| `GET` | `/bg-multi-stage-approval/closure-verifications` | ABEX-only closure verifications | abex role |
| `GET` / `POST` | `/bg-ceo-cfo-mail` | Attach offline CEO/CFO evidence | creator role |
| `GET` | `/bg-status` | Company-wide BG list with server-side filters | Login required |
| `GET` | `/bg-bank-tracker` | Bank authenticity verification (auto-triggered when BG goes Live) | Login required |
| `GET` | `/bg/<bg_id>` | Canonical BG record + workflow timeline | RBAC-checked |
| `POST` | `/bg/<bg_id>/generate-report` | Generate Comparison Report (deterministic PDF) | RBAC-checked |
| `GET` | `/bg-extension` | Extension management (urgency sections) | coordinator role |
| `GET` / `POST` | `/bg-closure` | Closure initiation (auto-computed eligibility) | coordinator role |
| `GET` | `/bg-closure-category-lead` | TC Head reviews exception closures | tc_head role |
| `GET` / `POST` | `/bg-return` | BG return (requested → dispatched → receipt-confirmed) | coordinator + limited creator |
| `GET` / `POST` | `/bg-invocation` | Claim window monitor + in-progress invocation | BU FC primary; TC Head hold-support |
| `GET` | `/documents/<id>` | Authenticated, ownership-checked document streaming | RBAC-checked |
| `GET` | `/documents/<id>/raw` | Same, without chrome | RBAC-checked |
| `GET` | `/documents/generated` | Versioned Comparison Report archive | RBAC-checked |
| `GET` | `/assistant/` | Q&A assistant full page | Login required |
| `GET` | `/notifications/` | In-app inbox | Login required |
| `GET` / `POST` | `/profile/` | Account / roles / preferences / notifications | Login required |
| `GET` | `/admin/` | Admin dashboard | admin only |
| `GET` | `/admin/users` | User & role management | admin only |
| `GET` / `POST` | `/admin/configuration` | Structured editors for `application_settings` | admin only |
| `GET` | `/admin/audit-log` | Server-side filtered audit log + CSV export | admin only |
| `GET` / `POST` | `/admin/prohibited-overrides` | Prohibited-clause admin override | admin only |
| `GET` | `/executive-approval/<token>` | Magic-link executive sign-off (used by closure / invocation / bank) | Token (rate-limited) |
| `GET` | `/api/pipeline/status/<bg_id>` | JSON pipeline status for polling | Owner |
| `POST` | `/api/pipeline/retry/<bg_id>/<stage>` | Stage-scoped manual retry | Owner |
| `GET` | `/api/po/context/<po_number>` | PO lookup (SAP or local fallback) | creator or coordinator |
| `GET` | `/api/closure/eligibility/<bg_id>` | Live-computed closure eligibility | coordinator |
| `GET` | `/api/parent-bg/search` | Search Live BGs to extend against | coordinator |
| `POST` | `/api/push/subscribe` | Register a Web Push subscription | Login required |
| `POST` | `/api/push/unsubscribe` | Remove a Web Push subscription | Login required |
| `GET` | `/api/push/vapid-public-key` | Expose the VAPID public key | Login required |

---

## ⚙️ Configuration

The app reads everything from environment variables (see [Environment Variables](#environment-variables)). There is **no separate config file** to edit - `bgcc/config.py` defines `DevelopmentConfig`, `TestingConfig`, and `ProductionConfig` classes and `FLASK_CONFIG` picks one.

Notable runtime-tunable settings live in `application_settings` (managed via the **Admin → Platform Configuration** UI):

| Setting key | What it controls |
|---|---|
| `doa_matrix` | Stage sequences for CAPEX/OPEX, CEO/CFO trigger tier, per-role deviation visibility |
| `active_clause_template` | Repeatable clauses, mandatory clause list |
| `prohibited_clause_patterns` | Regex-validated patterns that floor `effective_tier` to `prohibited` |
| `checklist_definitions` | Format-checklist sections and items |
| `approved_banks` | Issuing-bank master list (name, short code, contact email) |
| `extension_policy` | `warning_days` (45), `overdue_days` (21) |
| `invocation_policy` | `approaching_days` (60), `critical_days` (14) |
| `executive_contacts` | `cfo_email`, `ceo_email` for magic-link sign-off |
| `executive_approval_expiry_hours` | Default 72h |
| `bank_verification_expiry_hours` | Default 48h |
| `policy_reference_content` | Knowledge base for the Q&A assistant |

Every save increments `version`, sets `changed_by`, requires a `change_reason`, and writes a `config_changed` audit row.

---

## 🧪 Testing

There is **no formal test suite checked in** at this commit. The Step 8 notes describe a 12-scenario end-to-end walkthrough that was executed against the running integrated application (onboarding, CAPEX/OPEX intake-to-Live, elevated-risk via CEO/CFO mail, prohibited-block + admin override, extension, standard + exception closure with magic-link chain + SoD, return, invocation dual-gate, bank verification, bilingual grounded Q&A, and admin config/audit export) - all passing with no cross-step integration gaps.

If you want to add tests, the natural seams are:
- `bgcc/services/*_service.py` - pure-Python business logic, easy to unit-test
- `bgcc/services/prohibited_clauses.py` - deterministic tier-flooring rules
- `bgcc/services/workflow_service.py` - DoA matrix evaluation
- `bgcc/services/magic_link_service.py` - token issue/resolve/consume
- `bgcc/utils/numbers.py` - number-to-words

A `TestingConfig` is wired up in `bgcc/config.py` (uses a separate `bgcc_test.db`, disables CSRF, sets `CELERY_TASK_ALWAYS_EAGER=True`).

---

## 🌐 Deployment

### Gunicorn behind a reverse proxy

The app ships with a `wsgi.py` for Gunicorn. Recommended setup:

1. Place **nginx** (or another reverse proxy) in front to handle TLS termination and set `X-Forwarded-Proto` / `X-Forwarded-Host`.
2. Run **Gunicorn** with multiple workers.
3. Run a **Celery worker** process.
4. Run a **Celery Beat** process for scheduled jobs.
5. Use **Redis** as the Celery broker.
6. Migrate to **PostgreSQL** for any non-trivial workload (`DATABASE_URL=postgresql+psycopg2://...`).

The `ProxyFix` middleware is already configured (one level) so URL generation works correctly behind a reverse proxy.

### Reference production run

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

### What gets scheduled (Celery Beat)

| Job | Schedule | Purpose |
|---|---|---|
| `maintenance.daily_expiry_scan` | 01:00 daily | Creates extension requests for Live BGs in the warning window; marks overdue items |
| `maintenance.daily_extension_digest` | 08:00 daily | One digest email per Coordinator summarizing open extension items |
| `maintenance.daily_claim_window_scan` | 02:00 daily | Creates/advances `bg_invocations` for BGs approaching their claim window |
| `maintenance.warm_dashboard_cache` | Every hour at :05 | Pre-computes portfolio aggregates into Flask-Caching |
| `maintenance.bank_verification_poll` | Every 30 min | Expires unanswered bank-verification tokens to `no_response` |

---

## 🤝 Contributing

This is an internal platform, but if you're a contributor:

1. **Fork** the repo and create a topic branch:
   ```bash
   git checkout -b feat/your-feature
   ```
2. **Follow the existing patterns**:
   - Blueprint per concern in `bgcc/routes/`
   - Service module in `bgcc/services/` for business logic
   - Celery task in `bgcc/tasks/`
   - Model in `bgcc/models/`
   - Enums added to `bgcc/models/enums.py`
3. **Keep `requirements.txt` unversioned** (project convention).
4. **Write audit events** for any new state-changing operation:
   ```python
   from bgcc.services import audit_service
   audit_service.record("your_event", actor_id=current_user.id, target_type=..., target_id=..., metadata_json={...})
   ```
5. **Honor the access decorators** - `@roles_required(...)` for role-scoped routes, `@admin_required` for admin-only.
6. **CSRF must be on** for every form/AJAX POST.
7. **Keep magic-link tokens hashed at rest** - never log them, never write them in plaintext.
8. **Commit** with a clear message and **open a Pull Request** describing the change, screenshots if UI, and any schema migration notes.

### Reporting bugs

Please open a GitHub issue with:
- Steps to reproduce
- Expected vs. actual behavior
- Log output (with the `X-Request-Id` header value)
- `.env` redacted (no secrets)
- Python / OS / browser version

### Requesting features

Open a GitHub issue with the `enhancement` label and describe:
- The user need
- The proposed UX
- Any data-model implications

---

## 🗺️ Roadmap

The eight-step build is **complete**. Possible future directions:

- [ ] **Real SAP deep integration** - full PO/contract/GR/IR reconciliation, not just PO lookup
- [ ] **Multi-tenant** - multiple orgs on one deployment, with per-tenant data isolation
- [ ] **eSignature integration** - DocuSign / Adobe Sign for the invocation letter sign step (currently a checkbox + manual upload)
- [ ] **SSO via Microsoft Entra ID** - the `User.microsoft_oid` column is already there
- [ ] **Mobile-native app** - the PWA foundation is in place; a native shell could layer on push + biometrics
- [ ] **Webhook fan-out** - let internal systems subscribe to `audit_log` events
- [ ] **Translation engine** - beyond English + Hindi, the Q&A assistant could use Gemini to translate on the fly
- [ ] **Test suite** - formal pytest coverage for the service modules (currently validated end-to-end but not unit-tested)
- [ ] **Multi-currency** - `currency` column already exists; a proper FX conversion layer is the next step

---

## 📄 License

This project does not currently ship with a `LICENSE` file. Based on the repository (no license metadata on the GitHub page), it should be treated as **all rights reserved / internal use only** until an explicit license is added.

If you're a third party who wants to use or study this code, please contact the author first.

---

## 🙏 Acknowledgements

This platform stands on the shoulders of some excellent open-source work:

- [Flask](https://flask.palletsprojects.com/) and the entire Pallets ecosystem (Flask-SQLAlchemy, Flask-Migrate, Flask-Login, Flask-WTF, Flask-Caching, Flask-Limiter)
- [Celery](https://docs.celeryq.dev/) for battle-tested distributed task queues
- [ReportLab](https://www.reportlab.com/) and [python-docx](https://python-docx.readthedocs.io/) + [docxtpl](https://docxtpl.readthedocs.io/) for deterministic document generation
- [Google Gemini](https://ai.google.dev/) via the [`google-genai`](https://pypi.org/project/google-genai/) SDK
- [scikit-learn](https://scikit-learn.org/) for the TF-IDF retrieval in the policy assistant
- [pywebpush](https://github.com/web-push-libs/pywebpush) for VAPID-signed browser push notifications
- [Bootstrap](https://getbootstrap.com/) for the UI baseline
- [Chart.js](https://www.chartjs.org/) for the portfolio dashboard visualizations

---

## 📬 Contact

**Author:** [shahram8708](https://github.com/shahram8708) on GitHub

If you have questions, want to report a security issue responsibly, or are interested in collaborating on a similar platform, please open an issue on the [GitHub repository](https://github.com/shahram8708/BG-Center).

Built with care, on a Sunday evening, and a lot of `pdb.set_trace()` calls. - _The BG Command Centre team_