# Payment feasibility

Status: **no live payment channel is approved.** Development and CI use the simulated provider. Sources were read
on 2026-10-23; re-check them before every go-live decision.

## 1. Waffo Pancake (merchant of record)

Sources: https://docs.waffo.ai/llms-full.txt (authentication, checkout session, webhooks, refunds, GraphQL, test mode,
fees, prohibited products), https://www.waffo.ai/.

### Integration facts used in code (`app/payments/waffo.py`)
| Item | Value |
|---|---|
| Base URL | `https://api.waffo.ai` (`WAFFO_API_BASE`) |
| Auth | headers `X-Merchant-Id`, `X-Timestamp` (unix s, −5 min/+1 min window), `X-Signature` = base64 RSA-SHA256 over `METHOD\nPATH\nTIMESTAMP\nbase64(sha256(body))` |
| Environment | each API key is bound to test or prod at creation; no header needed |
| Checkout | `POST /v1/actions/checkout/create-session` — `productId` (PROD_…), `currency`, optional `priceSnapshot{amount,taxIncluded,taxCategory}` (API-key only), `buyerEmail`, `successUrl` (absolute https), `expiresInSeconds`, `orderMerchantExternalId` (≤128), `metadata`, `language` (`en`, `zh-Hans`, …). Response `data.{sessionId, checkoutUrl, expiresAt}`. No cancel URL. |
| Webhook | header `X-Waffo-Signature: t=<ms>,v1=<b64>`; verify RSA-SHA256 over `t + "." + rawBody` with the dashboard **Webhook Public Key** for the environment; tolerance 45 min (retries replay the original header); `mode` = `test`/`prod`; dedupe on `eventType + eventId`; up to 3 retries; respond 2xx within 10 s |
| Events used | `order.completed` (amount: `data.chargedAmount`, fallback deprecated `amount`), `refund.succeeded`, `refund.failed` |
| Refund | `POST /v1/actions/refund-ticket/create-ticket` — `paymentId`, `reason`, `requestedAmount{amount,currency}`, `refundTicketMerchantExternalId`; merchant tickets start `processing`; one in-flight ticket per payment; never retry 4xx |
| Query | GraphQL `POST /v1/graphql` `payments(filter:{orderMerchantExternalId:{eq}}){id status refundStatus …}` — used read-only for reconciliation alerts |
| SDKs | Official: TypeScript `@waffo/pancake-ts`, Next.js helpers, Go. **No official Python SDK** → manual signing exactly as the documented Python example. |
| Test mode | separate test API key + test webhook key; test cards e.g. `2226 9000 0000 0110`; webhooks fire normally |

### Fees (docs "Fees")
3.9 % + $0.50 per successful card/wallet transaction (WeChat Pay 3.9 %, no fixed); failed 3DS / authorisation
$0.30 per attempt; refunds $1.00 each and the original fee is **not** returned; payouts 1 % (min $10); chargebacks
$25 + optional $10 representment. Channel `waffo` is seeded with fee 0.039 + 0.50 so per-order fee and gross
contribution are tracked; refund/chargeback fees are not yet booked automatically (issue).

### Product eligibility (docs "Prohibited & Restricted Products") — the critical constraint
| Category | Waffo status | Impact on this store |
|---|---|---|
| Third-party game cards / game currency / top-up / proxy purchase | **Prohibited** | Cannot be sold via Waffo |
| Prepaid code and **gift-card resale** | **Prohibited** | Cannot be sold via Waffo |
| Opening/topping up third-party AI service accounts for others | **Prohibited** | Cannot be sold via Waffo |
| eSIM / data-plan resale | **Prohibited** | — |
| Human-delivered services (person doing the work) | **Prohibited** | "Manual delivery" products that are really services are not eligible |
| Physical goods | **Prohibited** | — |
| Software key reselling ("no cracked, pirated, or key-reselling operations") | Restricted — must be properly licensed | Only own/authorised licenses |
| In-game virtual items | Restricted — must be publisher/authorised distributor | — |
| VPN/VPS, IPTV, subscriptions, AIGC, courses | Restricted (conditions) | Case by case |
| Crypto-adjacent businesses | Cards only, **never WeChat Pay**; wallets/custody prohibited | Offering USDT checkout may make the store "crypto-adjacent" — **unverified, ask Waffo** |

Enforcement in code: Waffo is offered for a product only if it has a `waffo_product_id`; in **live** mode additionally
`waffo_approved=true` (owner-only toggle, audited) and `WAFFO_LIVE_ENABLED=true`. Do not misdescribe categories.

## 2. USDT on EVM networks (self-custodial, read-only monitoring)

| Network | Chain ID | Gas | Token (contract, checked at docs.usdt0.to deployments) | Decimals | Confirmations (config) | Status |
|---|---|---|---|---|---|---|
| X Layer | 196 | OKB | USDT0 `0x779Ded0c9e1022225f8E0630b35a9b54bE713736` | 6 (verified by RPC `decimals()` before enabling) | 20 | config ready, **not verified live** |
| Polygon PoS | 137 | POL | USDT0 `0xc2132D05D31c914a87C6611C10748AEb04B58e8F` | 6 | 64 | config ready, **not verified live** |

Notes:
- On X Layer the legacy bridged "USDT" and USDT0 are different contracts; only the configured contract is accepted, never by name.
- RPC defaults are public endpoints (rate-limited, no SLA) → configure a paid RPC via `USDT_RPC_<NETWORK>` for production.
- Confirmation depths are conservative defaults, not measured; X Layer finality and Polygon milestone finality should be
  re-checked and tuned. Actual network fees were **not measured** (no transactions were sent).
- Receiving wallet: a **self-custodial** HD wallet (e.g. OKX Web3 Wallet / hardware wallet) exporting an account-level
  xpub. **OKX exchange deposit addresses are not suitable** (custodial, shared/rotating, not attributable per order).
  Whether the wallet app exposes the xpub must be verified by the operator (issue).
- No private keys/mnemonics on the server. Refunds and sweeping are manual, offline, recorded by tx hash.
- Testing without real assets: unit tests use a fake EVM node; a testnet run (X Layer testnet / Polygon Amoy with a mock
  ERC-20) is planned (issue).

## 3. Open legal / provider questions (blockers for live)
1. Which concrete products will be sold, and are they eligible under Waffo's list? Get written merchant-review approval.
2. Does accepting USDT at checkout affect Waffo eligibility ("crypto-adjacent")?
3. Is accepting stablecoins lawful for the operator's and customers' jurisdictions; KYC/AML/tax/reporting duties?
4. Consumer-protection rules for digital goods refunds (e.g. EU right of withdrawal waiver wording).
5. Supplier terms (e.g. 16688-type platforms): is automated purchasing via an official API permitted? No API → manual only.
