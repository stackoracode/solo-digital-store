# Progress log

Resume work by reading: `AGENTS.md` → `docs/architecture.md` → this file → `git log -5 --stat` → `git status`.
Only mark items done when their tests ran green in CI (link the run).

## Current phase
**Phase 1 — MVP vertical slice.** Started 2026-10-23.

## Done (code written)
| Area | Files | Verified by |
|---|---|---|
| Research & reuse decision (Dujiao-Next GPL-3.0/Go vs new FastAPI) | docs/architecture.md §1, ADR-001 | review |
| Project skeleton: conda env, pinned requirements, Docker, compose, CI, docs | environment.yml, requirements*.txt, Dockerfile, docker-compose.yml, .github/workflows | CI (see below) |
| Catalog + quick listing + SKUs + images (Pillow→WebP, local/R2) + translations | services/catalog.py, services/storage.py, admin/catalog.py | tests: flow |
| Versioned custom order forms with server validation + forbidden-field rules | services/forms.py | tests: security |
| Orders: snapshots, three status axes, secret tokens, signed e-mail links, lookup | services/orders.py, routes/orders.py | tests: security, flow |
| Payment adapter contract; mock; Waffo (signing, webhooks, refunds, gating); USDT (xpub, scanner, reorg) | payments/*, services/payments.py | tests: payments, waffo, usdt |
| Card-code inventory: encryption, dedupe, SKIP LOCKED claims, export (owner, audited) | services/inventory.py | tests: inventory |
| Fulfilment: auto / manual / supplier; procurement state machine; mock supplier | services/fulfillment.py, services/procurement.py, suppliers/* | tests: inventory, procurement |
| Refunds with fulfilment freeze; commission reversal | services/refunds.py, services/agents.py | tests: inventory, auth_agents |
| Durable job queue (outbox), worker, dead letters → review queue | jobs/* | tests: flow |
| Auth: e-mail OTP, Google/OIDC (Authlib), Telegram widget, staff password + TOTP | services/auth.py, services/oauth.py, routes/account.py | tests: auth_agents, security |
| Support tickets, review queue, audit log, metrics dashboard, settings, AI provider registry, domains | services/*, routes/admin/* | partial tests |
| i18n en-US / zh-CN | scripts/build_locales.py, app/locales | tests: flow |

## Test results
- Local: `ruff check` + `ruff format --check` pass. Python byte-compiles. **pytest has not been run locally**
  (by decision: no local environment; CI runs it against PostgreSQL 16).
- CI: _pending first run — update this line with the run URL and result._

## In progress / next highest-value work
1. Get CI fully green (tests, mypy, migrations, audit, gitleaks); generate the initial Alembic migration via the
   "Autogenerate migration" workflow and commit it (add `crypto_address_index_seq` manually).
2. Staging deployment recipe (single VM + Cloudflare) and a smoke test against Waffo **test mode**.
3. USDT testnet end-to-end (mock ERC-20 on a testnet or anvil in CI).
4. Admin zh-CN coverage for dense tables; e-mail HTML templates.

## Blockers needing the owner (cannot be done by code)
- Decide product categories and obtain **Waffo merchant review approval**; create test + live API keys, copy webhook
  public keys, create Waffo products (PROD_…) and map them.
- Provide a self-custodial wallet **account xpub** (not seed/xprv) for USDT; choose RPC provider; legal check for crypto acceptance.
- Domain, Cloudflare account (WAF/Turnstile/R2), SMTP provider, Google OAuth client, Telegram bot (optional).
- Legal texts (terms, privacy, refund) for the actual sales regions.

## Important file locations
Payments `app/services/payments.py` · USDT scanner `app/payments/usdt/scanner.py` · Waffo `app/payments/waffo.py` ·
fulfilment `app/services/fulfillment.py` · procurement `app/services/procurement.py` · queue `app/jobs/queue.py` ·
access control `app/services/orders.py::order_by_token`, `app/web/routes/orders.py::access_order` · edge `app/core/middleware.py`.

## Recent commits
_(updated after each milestone)_
