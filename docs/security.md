# Security

Default assumption: every visitor, agent, webhook sender and upstream response may be hostile.

## Threat model (STRIDE summary)
| Threat | Example | Controls |
|---|---|---|
| Spoofing | forged payment webhook; fake Host header; stolen session | provider signature verification + env/mode check; host allow-list (421); httpOnly/SameSite cookies, server-side revocable sessions, session rotation on login; staff TOTP |
| Tampering | change price/amount client-side; form injection | server-side price from DB snapshot; amounts re-checked on payment; strict form schema validation; CSRF HMAC tokens + Origin check |
| Repudiation | admin denies refund/credit | `audit_logs` for every admin mutation incl. IP; order timeline |
| Information disclosure | enumerate order numbers; read other users' codes; cross-store leaks | order access needs secret token / signed e-mail key / owner session **and** matching store; codes encrypted; `no-store` + `no-referrer` on sensitive pages; masked codes in admin; reveal/export audited |
| DoS / cost abuse | checkout spam, e-mail bombing, huge uploads, RPC/AI cost | Cloudflare WAF/rate limits + Turnstile; app rate limits (Redis); body size limits; per-recipient e-mail caps; import caps; image pixel limits; AI budgets (config) |
| Elevation | customer → admin; OAuth account takeover | roles checked per route; external identities never enter staff accounts; auto-link only verified e-mails; owner-only for payments/settings/exports/credits |

## Sensitive data inventory
| Data | Where | Protection |
|---|---|---|
| Card codes / delivered content | `card_codes.secret_enc`, `deliveries.content_enc` | Fernet (MultiFernet rotation), keys in env only |
| Order access tokens, ticket tokens, session tokens | hashed (SHA-256) in DB | 24–32 byte random |
| Staff passwords | argon2id | lockout after 8 failures / 15 min |
| TOTP secrets, AI API keys | Fernet encrypted | write-only in UI |
| Login codes | keyed HMAC in DB; encrypted in job payload | 10-min TTL, 5 attempts, 5/h per e-mail |
| Customer e-mail, IP, form data | DB | minimal collection; visitor metrics use keyed hashes |
| Payment secrets (Waffo private key, webhook keys), USDT xpub, SMTP, R2 | environment only | never in DB/repo/logs |
| Wallet private keys / mnemonics | **nowhere in this system** | offline custody |

## Key handling
- Generate: `SECRET_KEY`/`FINGERPRINT_KEY` with `secrets.token_urlsafe(48)`; `DATA_ENCRYPTION_KEYS` with `Fernet.generate_key()`.
- Rotation: prepend a new Fernet key (old ones keep decrypting); re-encrypt job is future work. Rotating `SECRET_KEY`
  invalidates CSRF tokens and e-mailed signed links; rotating `FINGERPRINT_KEY` breaks dedupe of existing codes (avoid).
- Production refuses to start with default secrets, mock payments, missing Turnstile, non-HTTPS base URL or insecure cookies.

## Application controls implemented
Object-level access control · parameterised SQL (ORM only) · server-side validation · Jinja autoescape + `nl2br` escaping ·
CSP (`script-src 'self'` + Turnstile/Telegram, `frame-ancestors 'none'`, no inline JS/CSS) · HSTS when secure cookies ·
CSRF HMAC tokens + Origin check · staff TOTP enforced · audit logs · image re-encoding · webhook signatures + dedupe ·
rate limits · secrets redaction in logs · SSRF guard for admin-configured URLs · trusted-proxy-aware client IP · request
size limits · open-redirect guard · CSV formula-injection guard on exports.

## Cloudflare policy (to configure; not automatable from this repo)
- Proxy all public hostnames (orange cloud); origin firewall accepts 80/443 **only from Cloudflare IP ranges**; set
  `TRUSTED_PROXY_CIDRS` to those ranges and `TRUST_CF_CONNECTING_IP=true`.
- WAF managed rules on; rate-limit rules: `/p/*/checkout`, `/lookup`, `/account/login*`, `/account/staff-login` (e.g. 10/min/IP);
  `/webhooks/*` exempt from challenges but restricted by method + size.
- Turnstile widget keys in env; tokens verified server-side (`core/turnstile.py`).
- Cache: `/static/*` and R2 public images long TTL; never cache `/o/*`, `/account*`, `/admin*`, `/support/t/*` (app sends `no-store`).
- Admin: additionally protect `/admin*` and `/account/staff-login` with Cloudflare Access (recommended).

## Payment security rules
Only verified provider evidence or verified on-chain receipts mark orders paid; success pages only display status.
Webhook processing is idempotent; duplicate/late/under/over payments go to review. No outbound crypto transfers exist in
code. Live channels require explicit env gates and (Waffo) per-product approval.

## Prohibited high-risk operations (humans and AI agents)
Storing seed phrases/private keys; giving AI access to payment keys or unlimited procurement; letting AI confirm payments,
promise refunds or disclose other customers' data; scraping/bypassing supplier logins; enabling live payments for unapproved
products; disabling CI security checks; force-pushing over history.

## Emergency shutdown runbook
1. **Stop taking money:** Admin → Payments → disable channels (or set `WAFFO_LIVE_ENABLED=false` / `USDT_LIVE_ENABLED=false` and restart).
2. **Stop deliveries:** scale `worker` to 0 (jobs stay queued and resume safely later).
3. **Contain:** Cloudflare "Under Attack" mode / block IPs from access logs (JSON logs include `ip`); revoke sessions
   (`UPDATE user_sessions SET revoked_at=now()`), rotate `SECRET_KEY`.
4. **Credentials:** rotate Waffo API key + webhook keys in dashboard, SMTP, R2, AI keys; if data keys are suspected leaked,
   add a new Fernet key and plan re-encryption; revoke affected card codes (void) and re-stock.
5. **Investigate:** `audit_logs`, `payment_events` (incl. invalid signatures), `review_items`, `jobs` (dead).
6. **Communicate:** affected customers via tickets/e-mail; record the incident in `docs/progress.md`.

## Verification pipeline
CI: ruff (incl. bandit-style `S` rules), mypy, pytest with security tests (authorization, CSRF, host spoofing, SSRF,
signatures, double-spend/double-delivery), pip-audit, gitleaks. Planned: OWASP ZAP baseline against staging (issue).

## Accepted dependency advisories
| ID | Package | Why accepted | Revisit |
|---|---|---|---|
| PYSEC-2026-1325 / CVE-2024-23342 (Minerva) | `ecdsa` (transitive via `bip-utils`) | Affects P-256 signing/key generation timing. The app only derives secp256k1 *public* keys from an xpub; no private keys exist on the server. No upstream fix planned. | If any signing is ever added, or bip-utils drops/changes the dependency |
