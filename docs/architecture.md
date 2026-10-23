# Architecture

Self-operated digital-goods store with agent (affiliate) distribution. Modular monolith: one FastAPI web
process plus one worker process, PostgreSQL as the system of record and job queue, Redis for rate limits.

## 1. Reuse assessment (2026-10-23)

| Candidate | License | Stack | Assessment |
|---|---|---|---|
| **dujiao-next/dujiao-next** (1.6k★, pushed 2026-10-13, 1,162 Go files, 379 `_test.go`) | **GPL-3.0** | Go (Gin, GORM, asynq/Redis, casbin, pquerna/otp), Vue admin + user SPA | Most complete reference: modules `catalog, cardsecret, order, payment, procurement, reseller, affiliate, wallet, reconciliation, compliance, upstreamapi, telegram…`; payment gateway contract (`internal/modules/payment/contract/gateway.go`) with alipay/epay/epusdt/bepusdt/dujiaopay adapters; very active (multiple fixes/day). |
| Dujiao (classic, PHP/Laravel) | MIT | PHP | Unmaintained lineage; superseded by Dujiao-Next. |
| "Other Dimension" (异次元) store | proprietary/unclear | PHP | No clear license → not reusable. |

**Decision (ADR-001):** build a new FastAPI project, reuse *ideas* (not code) from Dujiao-Next.
Reasons: (a) the owner explicitly asked for a Python/FastAPI backend managed by conda; (b) Dujiao-Next is Go +
two Vue SPAs (~10k files) — taking it over means maintaining a large Go/Vue codebase alone; (c) it has no Waffo
adapter and its USDT adapters integrate third-party *epusdt/bepusdt* services (amount-tail matching), which conflict
with our "no fictitious decimal amounts" and watch-only-xpub requirements; (d) GPL-3.0 would require publishing
derivative source — acceptable for a public repo, but there is no benefit without code reuse. No Dujiao-Next code
is copied; therefore no GPL obligations arise. Ideas adopted and credited: gateway contract shape
(create/query/callback), card-secret batches + stats, procurement-order states with "already charged upstream"
handling, commission reversal on refund, login credentials bound to distribution site.

## 2. Technology choices

| Concern | Choice | Why (alternatives) |
|---|---|---|
| Web | FastAPI + Jinja2 SSR | One process, fast mobile first paint, SEO for product pages, no Node runtime (Next.js SPA rejected: second runtime, more cost). JSON API kept small (`/api/v1`) for future clients. |
| DB | PostgreSQL 16, SQLAlchemy 2.0 sync, Alembic | Row locks, partial unique indexes, `SKIP LOCKED`. Sync sessions = simpler, fewer bugs; FastAPI runs sync handlers in a threadpool. |
| Jobs | Postgres `jobs` table (ADR-002) | Transactional with business writes (outbox for free), retries/backoff, dead-letter → review queue, visible in admin. Celery/Dramatiq need Redis + a separate outbox; Procrastinate considered (good fit) but sharing a SQLAlchemy transaction needs extra glue — can swap later behind `jobs.queue`. |
| Auth | Server-side sessions; e-mail OTP; Authlib for Google/OIDC; Telegram widget; staff password + TOTP (ADR-007) | Logto/Better Auth evaluated, see ADR-007. |
| Crypto | `cryptography` Fernet (MultiFernet rotation), keyed HMAC fingerprints, argon2id | Standard, audited libs. |
| USDT | Read-only JSON-RPC + `bip-utils` xpub derivation (ADR-004) | No custody, no private keys on server. |
| Images | Pillow re-encode → WebP → local disk (dev) / Cloudflare R2 (prod) | Content-type validation by decoding, strips EXIF. |
| Front | Hand-written CSS (~10 KB) + 2 KB JS | No build step; strict CSP (`script-src 'self'`). |

## 3. Modules and boundaries

```
web (routes, templates) ──► services ──► models / core
                               │
                               ├─► payments (adapter registry: mock | waffo | usdt)
                               ├─► suppliers (adapter registry: mock | <authorised APIs>)
                               └─► jobs.queue.enqueue (same transaction)
worker ─► jobs.handlers ─► services
```

| Module | Files | Owns |
|---|---|---|
| catalog | `services/catalog.py`, `models/catalog.py` | categories, products, SKUs, images, translations |
| forms | `services/forms.py` | versioned custom order forms + server validation |
| orders | `services/orders.py`, `models/orders.py` | order + item snapshot, access tokens, expiry |
| payments | `services/payments.py`, `payments/*` | channels, attempts, events, apply success/failure |
| inventory | `services/inventory.py`, `models/inventory.py` | encrypted codes, batches, dedupe, claiming |
| fulfillment | `services/fulfillment.py` | auto / manual / supplier routing, deliveries |
| suppliers/procurement | `services/procurement.py`, `suppliers/*` | upstream orders, idempotency, caps, takeover |
| refunds | `services/refunds.py` | refund lifecycle, fulfilment freeze |
| agents | `services/agents.py`, `models/stores.py` | agents, attribution, commissions; stores/domains (future) |
| support | `services/support.py` | tickets, messages |
| users/auth | `services/auth.py`, `services/oauth.py` | sessions, OTP, identities, MFA |
| finance/metrics | `services/metrics.py` | KPIs, fees, gross contribution |
| system | `services/settings_store.py`, `models/ops.py` | settings, audit, AI providers, review queue, jobs |

## 4. Key data model

- `orders` (three status axes: `payment_status`, `fulfillment_status`, `after_sale_status`, plus `lifecycle`),
  `order_items` (immutable `snapshot` of title/price/currency/fulfilment rules/refund policy/form fields + `form_version_id` + validated `form_data`),
  `order_events` (timeline; `visible_to_customer`), `deliveries` (`card_code_id` **unique**), `refunds`.
- `payment_channels` (non-secret config, enabled, mode test|live, fees), `payment_attempts` (amount/currency snapshot,
  pay amount/currency, FX snapshot, expiry, provider refs, status; **partial unique index: one active attempt per order+channel**;
  unique `(provider, provider_payment_id)`), `payment_events` (unique `(provider, event_type, event_id)`).
- `crypto_addresses` (unique address, unique derivation index, one attempt each, never reused),
  `chain_transfers` (unique `(chain_id, tx_hash, log_index)`), `chain_cursors`.
- `card_codes` (`secret_enc`, unique `(sku_id, fingerprint)`), `card_batches`.
- `procurements` (unique `order_item_id`, unique `idempotency_key`, status, cost cap, log), `jobs`, `review_items`.
- `stores`, `store_domains`, `agents`, `commissions` (unique `order_id`), `users`, `user_sessions`,
  `user_identities` (unique `(provider, subject)`), `email_login_codes`, `tickets`, `ticket_messages`,
  `audit_logs`, `settings`, `ai_providers`, `visitor_daily`, `email_logs`.

## 5. Order state machine

```
payment:     unpaid ──(verified evidence only)──► paid ──► refund_pending ──► partially_refunded | refunded
                                                    ▲             │ (refund failed)
                                                    └─────────────┘
fulfilment:  not_started ─► queued ─► delivered
                              ├─► awaiting_stock   (review: out_of_stock)
                              ├─► awaiting_manual  (review: manual_delivery)
                              ├─► awaiting_supplier ─► delivered | needs_review
                              └─► canceled (refund before delivery)
after-sale:  none ─► open (ticket) ─► resolved
lifecycle:   pending_payment ─► active ─► completed ;  pending_payment ─► expired | canceled
```
All transitions that touch money or goods take `SELECT … FOR UPDATE` on the order row first (lock order:
order → attempt → procurement/transfer). Fulfilment proceeds only when `payment_status == paid`, so a refund
request (which sets `refund_pending` under the same lock) blocks any later delivery.

## 6. Payment Provider Adapter

`app/payments/base.py`: `check_available(channel, order)`, `create_payment(db, channel, order, attempt) -> CreateResult`,
`parse_webhook(channel, headers, body) -> WebhookEvent` (must verify authenticity), `refund(channel, attempt, refund) -> RefundResult`.
`services/payments.handle_webhook` stores every event (invalid ones too), dedupes, maps it to an attempt, then calls
`apply_payment_success`, which checks currency, under/over-payment, lateness, duplicates (second paid attempt ⇒
`duplicate_payment` review + refund), records fees, enqueues fulfilment + e-mail and creates the commission — all in one transaction.

- **mock**: dev/CI only, HMAC-signed events through the same path; refused in production.
- **waffo**: see `docs/payment-feasibility.md`; RSA-SHA256 request signing, `t.body` webhook verification with
  per-environment public key, `mode` must match channel mode, `orderMerchantExternalId = attempt_no`.
  A read-only GraphQL reconcile job flags "paid upstream but no webhook" for humans; it never credits.
- **usdt**: see ADR-004 and §8.

## 7. Card-code inventory and procurement

- Import: normalise lines, keyed HMAC fingerprint, `INSERT … ON CONFLICT DO NOTHING` on `(sku_id, fingerprint)`, Fernet-encrypt.
- Claim: `SELECT … WHERE status='available' ORDER BY id LIMIT n FOR UPDATE SKIP LOCKED`, all-or-nothing, inside the
  order-locked fulfilment transaction; `deliveries.card_code_id` unique is the last line of defence.
- Procurement (ADR-005): `pending → submitted` is **committed before** calling the supplier; timeout/ambiguous ⇒
  `unknown` + query job; `not_found` re-submits only when the adapter declares `idempotent_submit`; price cap checked
  before submit and after result; refund race keeps goods hidden (`revoked_at`) and opens a review. A periodic sweep
  re-queues stale `submitted/unknown` rows if a follow-up job was lost.

## 8. USDT receipt design (ADR-004)

Per attempt a fresh address `m/44'/60'/0'/0/i` is derived from the account **xpub** (`i` from a DB sequence; never reused,
watched 30 days). Amount = order USD total ÷ rate (peg or CoinGecko with 0.97–1.03 sanity band), rounded **up to whole cents**,
snapshotted with source/time. Scanner per network every 15 s: chain-id guard → `eth_getLogs(Transfer, to ∈ watched)` over
`[cursor − reorg_depth, head]` → only the configured contract is accepted → after N confirmations re-fetch the receipt and
require `status=1`, same block hash, same log content → credit. Edge cases:

| Case | Handling |
|---|---|
| duplicate log / overlapping rescans | unique `(chain_id, tx_hash, log_index)` |
| underpaid | accumulates; remaining shown to buyer; at expiry ⇒ `needs_review` |
| overpaid | paid + `overpaid` review (refund difference) |
| second payment after paid | `extra_transfer` review |
| late (after expiry + 5 min) | `late_transfer` review; owner can verify & credit |
| wrong token / look-alike "USDT" | ignored (contract allow-list); support process |
| wrong chain (same EVM address) | not detected; funds recoverable offline by key holder — support process |
| reorg / dropped tx | receipt missing ×3 or block hash changed ⇒ `reorged`, never credited |
| RPC failure / wrong chain id | scan aborts, cursor not advanced, error shown on channel; multiple RPC URLs |
| missed logs | `balanceOf` reconciliation ⇒ `balance_mismatch` review; admin rescan from block |
| user-submitted tx hash/screenshot | not accepted as evidence at all |

## 9. Agents, stores and domains (ADR-006)

Phase 1: `?ref=CODE` → 30-day last-click cookie → validated at checkout (active agent, not self) → commission row
created atomically with payment → reduced/reversed on refund (clawback note if already paid out). Funds always go to
the single self-operated merchant account. Future storefronts: `stores` + `store_domains`. Requests for any host other
than `PRIMARY_HOSTS` or a **verified** domain get 421; generated links always use `PUBLIC_BASE_URL`; cookies are
host-only; every order/ticket lookup is filtered by the resolved `store_id`. Cloudflare for SaaS custom hostnames
will provide TLS + ownership validation (issue #roadmap).

## 10. Trust boundaries & data flow

```
Browser ──HTTPS──► Cloudflare (WAF, Turnstile, rate limit) ──► uvicorn (origin allow-list, EdgeMiddleware)
   │                                                               │
   └─ hosted checkout ◄── Waffo ──signed webhook──────────────────►│  verify → payment_events → attempts → orders
EVM RPC (read-only) ◄──── worker scanner ─────────────────────────►│
Supplier API (authorised only) ◄── worker procurement ────────────►│
SMTP ◄── worker e-mail                                             PostgreSQL (system of record + jobs)
```
Untrusted: all browser input, Host/X-Forwarded-For (unless from `TRUSTED_PROXY_CIDRS`), webhooks until verified,
RPC responses (cross-checked by receipt + chain id), supplier responses (caps, quantity checks).

## 11. Deployment

Single VM or container host: `web` (uvicorn, 2–4 workers behind Cloudflare), `worker` (1–2 replicas, safe via
SKIP LOCKED), managed PostgreSQL with PITR backups, small Redis. `alembic upgrade head` before rollout. Environments:
development (compose, mock payments), test/CI (GitHub Actions, Postgres service), staging (Waffo test mode, testnets),
production (`ENV=production` enforces secure config; mock disabled). The public GitHub repo is code only.

## ADRs

- **ADR-001 New FastAPI codebase instead of Dujiao-Next fork.** Context §1. Alternatives: fork Dujiao-Next (Go, GPL),
  Next.js + FastAPI. Decision: FastAPI SSR monolith. Trade-off: fewer ready features (coupons, wallet, Telegram bot) —
  re-implemented only when needed; much smaller surface to maintain.
- **ADR-002 Postgres job table as outbox + queue.** Alternatives: Celery/Redis (+ separate outbox), Procrastinate, arq.
  Decision: own ~200-line queue with dedupe keys, backoff, dead-letter→review, reaper, admin UI. Trade-off: throughput
  limited to Postgres polling (fine for <100 jobs/s); revisit with Procrastinate if load grows.
- **ADR-003 Server-rendered UI.** Alternatives: Vue/Next SPA. Decision: Jinja2 + progressive enhancement. Trade-off:
  less app-like admin; acceptable for a solo operator, far lower cost and attack surface.
- **ADR-004 Watch-only per-order addresses for USDT.** Alternatives: single address + unique amount tails (epusdt style,
  rejected: fictitious decimals, ambiguous matching), exchange deposit addresses (rejected: not designed for merchant
  attribution, custodial), hosted crypto processors (possible future channel). Decision: xpub derivation + own scanner.
  Trade-off: funds sit on many addresses; sweeping needs gas and an offline signing process (not built; issue).
- **ADR-005 Procurement never blindly retries.** Persist intent before submit; resolve unknown by query.
- **ADR-006 Single merchant of record.** Agents earn commission only; no split payments or payouts handled for third parties.
- **ADR-007 Login: in-app Authlib now, Logto-ready.** Requested: guest checkout + e-mail code + Google + Telegram,
  long-term multi-domain. Options: (a) **Logto** (MPL-2.0, self-hosted OIDC IdP, multi-tenant/custom domains) — best for
  many agent domains later, but another service + database to run and secure now; (b) **Better Auth** — TypeScript library,
  would require a Node runtime beside FastAPI (rejected); (c) in-app: Authlib OIDC client (Google), Telegram widget HMAC
  check, e-mail OTP, provider-agnostic `user_identities`. Decision: (c) with a **generic OIDC slot** (`OIDC_*` env) so a Logto
  tenant can be plugged in later without code changes. Rules: identities auto-link only on provider-verified e-mail; external
  logins never grant or enter staff accounts; staff use password + TOTP.
