# Business model

## Canvas — self-operated digital goods store (Phase 1)
| Block | Content |
|---|---|
| Customer segments | Global buyers of legitimately resellable digital goods (own/authorised licenses, digital templates, services delivered as digital content); mobile-first, low patience, often first-time visitors. |
| Value proposition | Instant or clearly time-boxed delivery, transparent price/currency/refund terms before payment, guest checkout, human support. |
| Channels | Storefront (SEO product pages), agent referral links, social/IM sharing. |
| Customer relationships | Order page + e-mail link, tickets linked to orders, optional passwordless account. |
| Revenue | Product margin. Agents earn a commission only on paid, non-refunded orders. |
| Key resources | Inventory (card codes), authorised suppliers, payment approvals, reputation. |
| Key activities | Sourcing, listing, support, fraud/risk review, reconciliation. |
| Key partners | Waffo (MoR, cards/wallets), self-custodial wallet + RPC provider, Cloudflare, SMTP, authorised suppliers. |
| Cost structure | Hosting (~1 small VM + managed Postgres), Waffo 3.9 % + $0.50 (+ $1/refund, $0.30 failed 3DS), RPC, e-mail, supplier cost, commissions, own time. |

## Agent distribution (Phase 1 → later)
Phase 1: invited/approved agents, referral link, single-level percentage commission, reversal on refund, manual payout
outside the platform. Later: agent storefronts on own domains (stores/domains model exists), agent copy/contacts.
Never: agents changing payment accounts, multi-merchant split payments, collecting funds for third parties.

## Unit economics (example, to validate with real data)
$10 order via Waffo card: fee $0.89 → if product cost $6 and 10 % commission ($1) → contribution ≈ $2.11 (21 %).
A refund costs an additional $1 and the original fee. At 1–2 % visitor conversion, 1,000 visitors ≈ 10–20 orders.
Low-priced items (<$5) are hurt by the $0.50 fixed fee → prefer USDT or bundles for them.

## MVP metrics (Admin dashboard)
Visitors (unique/day, keyed hash) · orders · paid orders · conversion · payment success rate (attempts) · delivery rate ·
average paid→delivered time · refunds (count/amount) · channel fees · procurement cost · commissions · gross contribution ·
open review items. All computed with Decimal.

## Explicitly out of scope for the MVP
C2C marketplace / third-party merchants · multi-level distribution · coupons/marketing automation · recommendation feeds /
community · wallet balances / stored value · automated payouts · advanced financial reports · AI auto-replies (config only) ·
automatic sweeping of USDT addresses · automated Cloudflare custom-hostname provisioning.
