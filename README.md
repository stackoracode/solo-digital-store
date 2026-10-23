# Solo Digital Store

A self-operated digital-goods store with agent (affiliate) distribution, built as a small, secure **FastAPI modular
monolith**: mobile-first storefront, guest checkout, passwordless accounts (e-mail code · Google · Telegram · any OIDC
such as Logto), encrypted card-code inventory with automatic delivery, manual and supplier fulfilment, Waffo and USDT
(X Layer / Polygon, watch-only) payment adapters, support tickets, review queue, and an MFA-protected admin.
English (US) and Simplified Chinese UI.

> **Status:** MVP in development. Only the simulated payment channel is usable; **no live payment channel is approved**.
> See [`docs/progress.md`](docs/progress.md) for what is verified.

## Quick start (development stack, simulated payments)
```bash
cp .env.example .env
docker compose up --build              # web :8000, worker, postgres, redis, mailpit :8025
docker compose exec web python -m app.cli seed-demo
printf '%s\n' 'choose-a-long-password' | docker compose exec -T web python -m app.cli create-admin you@example.com
```
Open http://localhost:8000 (shop) and http://localhost:8000/account/staff-login (admin; TOTP setup on first login).

Conda (for running tools/tests outside Docker):
```bash
conda env create -f environment.yml && conda activate solostore
TEST_DATABASE_URL=postgresql+psycopg://store:store@localhost:5432/store_test pytest
```

## Documentation
| Doc | Content |
|---|---|
| [AGENTS.md](AGENTS.md) | Engineering rules for humans and AI agents |
| [docs/architecture.md](docs/architecture.md) | Architecture, modules, state machines, ADRs, reuse assessment |
| [docs/payment-feasibility.md](docs/payment-feasibility.md) | Waffo rules/API/fees, USDT networks, open legal questions |
| [docs/security.md](docs/security.md) | Threat model, sensitive data, Cloudflare policy, emergency runbook |
| [docs/business-model.md](docs/business-model.md) | Business model canvas, metrics, out-of-scope list |
| [docs/progress.md](docs/progress.md) | Verified progress log and next steps |
| [docs/requirements.md](docs/requirements.md) | Structured product requirements |

## Prior art and attribution
Design ideas (not code) were taken from [Dujiao-Next](https://github.com/dujiao-next/dujiao-next) (GPL-3.0, Go/Vue):
payment gateway contract, card-secret batches, procurement states, commission reversal. No Dujiao-Next source code is
included in this repository. Waffo integration follows the official docs at https://docs.waffo.ai. USDT0 contract
addresses come from https://docs.usdt0.to. See `docs/architecture.md` §1 for the comparison.

## Environments
`development` (compose, mock payments) · `test` (CI) · `staging` (Waffo test mode, testnets) · `production`
(`ENV=production` refuses insecure settings). This public repository contains code only — no credentials, customer
data or production configuration.

## License
MIT — see [LICENSE](LICENSE).
