# AGENTS.md — rules for Claude Code and other coding agents

Read this file, `docs/architecture.md`, `docs/progress.md` and `git log -5 --stat` and run `git status`
before changing anything. Update `docs/progress.md` (and any affected doc) in the same commit as the code.

## Stack (do not swap without an ADR in docs/architecture.md)
- Python 3.12, FastAPI, SQLAlchemy 2.0 (sync sessions), Alembic, PostgreSQL 16, Redis (rate limits only).
- Server-rendered Jinja2 pages + one small vanilla JS file. No SPA, no inline JS/CSS (strict CSP).
- Durable jobs: PostgreSQL `jobs` table (transactional outbox + `FOR UPDATE SKIP LOCKED` worker).
- Auth: e-mail OTP, Authlib (Google / generic OIDC e.g. Logto), Telegram widget; staff password + TOTP.
- Env: conda (`environment.yml` → `requirements-dev.txt` → `requirements.txt`, all pinned). Docker uses `requirements.txt`.

## Layout
```
app/config.py            settings (env only)          app/db.py      engine/session/Base
app/core/                security, money, i18n, middleware, ratelimit, netsafe, turnstile, logging
app/models/              ORM models (one file per module)
app/services/            business logic (catalog, forms, orders, inventory, payments, fulfillment,
                         procurement, refunds, agents, support, auth, oauth, notify, metrics, storage)
app/payments/            provider adapters: base.py contract, mock, waffo, usdt/ (hd, rpc, scanner)
app/suppliers/           supplier adapter contract + mock
app/jobs/                queue.py (enqueue/claim/retry), handlers.py, worker.py
app/web/                 deps.py, templating.py, routes/ (shop, orders, account, support, agent,
                         webhooks, api, admin/*), templates/, static/
app/locales/             generated from scripts/build_locales.py — edit the script, not the JSON
alembic/versions/        migrations          tests/   pytest (needs PostgreSQL)
```

## Dependency rules
- `web` → `services` → `models`/`core`; `payments`/`suppliers` adapters may import `core`, `models`, `config` only.
- Order/fulfilment code must never branch on a specific payment provider; go through `app/payments/base.py`.
- Only `services/payments.apply_payment_success` may set an order to paid. Only `services/fulfillment`
  and `services/procurement` may create `Delivery` rows.
- Side effects that must survive a crash (fulfilment, procurement, refunds, e-mail) are enqueued with
  `jobs.queue.enqueue` **in the same transaction** as the state change. Never use `BackgroundTasks`/asyncio tasks for them.

## Conventions
- Money: `Decimal` + `MONEY` column type; never float (`core.money.to_decimal` rejects floats).
- Status fields: three axes on orders (payment / fulfilment / after-sale). Constants live in `models/orders.py`.
- Names: snake_case modules, `*_no` = public reference, `*_token_hash` = SHA-256 of a secret, `*_enc` = Fernet ciphertext.
- Every admin mutation calls `services.audit.audit(...)`. Every customer-visible order change calls `order_event(..., visible=True)`.
- UI strings: add to `scripts/build_locales.py` (en-US + zh-CN), run it, commit both JSON files.
- Style: ruff (line length 110), comments explain *why*; keep functions small; no new abstraction without a second user.

## Commands (run in CI; locally only if you have the conda env)
```
conda env create -f environment.yml && conda activate solostore
ruff check . && ruff format --check . && mypy
TEST_DATABASE_URL=postgresql+psycopg://store:store@localhost:5432/store_test pytest
alembic upgrade head && alembic check
python scripts/build_locales.py
docker compose up --build        # full stack, simulated payments
```
Schema change: edit models → push → run the **Autogenerate migration** workflow (Actions tab) →
download artifact → review (add `CREATE SEQUENCE` etc. that autogenerate misses) → commit to `alembic/versions/`.

## Security requirements (non-negotiable)
- Never commit secrets, `.env`, keys, seed phrases, customer data, card codes, logs. Secrets come from env only.
- Never store wallet private keys / mnemonics / xprv anywhere in this system. USDT is watch-only (xpub).
- Never mark an order paid from a redirect, front-end callback, screenshot or user-submitted tx hash.
- Webhooks: verify signature first, dedupe on (provider, event_type, event_id), then apply idempotently.
- Object access: orders/tickets need the secret token, a signed e-mail key, or the owning session — and the
  same `store_id`. Never authorize by `order_no` alone.
- Forms: server-side validation only counts; never add fields collecting passwords/card/PIN/seed data.
- Outbound URLs configured by admins go through `core.netsafe.validate_outbound_url`.
- Live payments: Waffo needs `WAFFO_LIVE_ENABLED` + per-product `waffo_approved`; USDT needs
  `USDT_LIVE_ENABLED` + RPC-verified network. Do not weaken these gates or misdescribe product categories.

## Prohibited actions for agents
- Initiating real charges/refunds/transfers, touching production servers or data, deleting data,
  force-pushing, rewriting history, disabling tests/CI checks to get green, scraping or bypassing supplier logins,
  guessing third-party API parameters (read the official docs and cite them in code comments).

## Commits
Conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`), one self-contained milestone per
commit, only after its validation ran. State honestly in the message/progress log if tests could not run.

## Task definition template (use in docs/progress.md and GitHub issues)
Goal · scope/out-of-scope · files/modules touched · acceptance criteria (tests or observable behaviour) · risks.
