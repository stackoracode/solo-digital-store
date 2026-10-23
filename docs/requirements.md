# Product requirements (structured from the owner's brief, 2026-10-23)

## Goal
Production-grade, low-cost, secure digital-goods store run by one person; later agents promote products and may run
storefronts on their own domains. Not a C2C marketplace.

## Phase 1 — MVP (must work end to end and pass tests)
1. Mobile-first storefront: home, categories, search, product detail (price, currency, delivery method, ETA, refund
   policy, support contact), en-US + zh-CN.
2. Guest checkout; optional passwordless account (e-mail code, Google, Telegram; OIDC/Logto-ready); order center;
   post-payment prompt to link orders (no mandatory extra data).
3. Secure guest order access: secret token or e-mailed signed link; lookup by order no + e-mail only e-mails the link.
4. Admin quick listing (name, price, description, delivery method; images optional) + full editor: SKUs, images,
   usage/purchase notes, translations, custom order form (text, number, radio, checkbox, select, textarea; label, hint,
   validation, required, max length, order), versioned and snapshotted into orders; no credential fields.
5. Card-code inventory: import/export (audited), de-duplication, batches, stock report, low-stock alerts, encrypted at
   rest, concurrency-safe automatic delivery.
6. Fulfilment per product: automatic card codes · manual delivery · supplier procurement (adapter + mock).
7. Payments: adapter architecture; simulated provider; Waffo test mode; USDT on-chain verification (testable without
   real assets); per-channel enable/disable/config/monitoring; attempts, events, refunds, fees, audit.
8. Order status on three axes; duplicate callbacks, repeated payment, late/under/over/duplicate payments, refund vs
   delivery races, browser closed after payment — all handled; ambiguity → manual review queue.
9. Durable async jobs with timeouts, retries, dead letters visible to admin.
10. Support: pre-sale contact, order-linked tickets, admin replies, e-mail notifications via queue.
11. Security baseline (see docs/security.md), metrics dashboard, legal pages (drafts), audit log.

## Phase 2+
Agent center (storefronts, own domains via Cloudflare for SaaS), upstream automated procurement with real authorised
APIs, AI-assisted support (drafts only, human approval), multi-currency pricing, sweeping/refund tooling for USDT,
richer reporting.

## Hard constraints
No live payments for unapproved product categories; no misrepresentation to payment providers; no custody of crypto keys;
no scraping or login bypass of suppliers; no AI with payment keys or unrestricted procurement; fixed costs minimal.
