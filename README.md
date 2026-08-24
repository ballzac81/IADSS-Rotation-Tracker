# IADSS Rotation Tracker

Pair rotator for the [IADSS Confluence Monitor](https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/). Same webhook / Telegram / Unraid style as [IADSS-Signal-Tracker](https://github.com/ballzac81/IADSS-Signal-Tracker), but a **separate Freqtrade** on a **Kraken subaccount**.

IADSS keeps SOL, HYPE, and TAO independent on the main account. This repo is only the HYPE/SOL rotation sleeve.

> **Spot only. Never shorts.** Long/Short is the ratio direction, not a leveraged short.

## How a rotation works

Chart the ratio (example: `HYPEUSD/SOLUSD` on TradingView). When IADSS completes a sequence:

| Signal | Meaning | Action (default 50%) |
|--------|---------|----------------------|
| **Long** | Base (HYPE) is stronger | Sell 50% of **SOL USD value** → buy HYPE with the proceeds |
| **Short** | Quote (SOL) is stronger | Sell 50% of **HYPE USD value** → buy SOL with the proceeds |

Sizing is always **USD of the weaker side**, not token count.

Example starting book: `$5,000 HYPE` + `$5,000 SOL`.

1. Long → sell `$2,500` of SOL, buy `$2,500` of HYPE → about `$7,500 HYPE` / `$2,500 SOL`
2. Long again → sell `$1,250` of remaining SOL → about `$8,750 HYPE` / `$1,250 SOL`
3. Short → sell `$4,375` of HYPE → about `$4,375 HYPE` / `$5,625 SOL`

You stay long both the whole time. Only the mix changes.

## Keep the books separate

| Sleeve | Kraken | Freqtrade | Signals |
|--------|--------|-----------|---------|
| IADSS independent | Main account | Existing `freqtrade` | SOL / HYPE / TAO pair alerts |
| HYPE/SOL rotation | **Subaccount** | `rotation-freqtrade` (this repo) | Ratio-chart Long / Short only |

Kraken extra API keys on the **same** account still share balances. Subaccounts do not.

Do **not** run `docker-compose.sidecar.yml` against live IADSS. That file is left only as a warning.

## Endpoints

| Endpoint | Method | Action |
|----------|--------|--------|
| `/confirm-long` | POST | Telegram early warning only |
| `/confirm-short` | POST | Telegram early warning only |
| `/rotate-long` | POST | Execute Long rotation |
| `/rotate-short` | POST | Execute Short rotation |
| `/rotate` | POST | Body `{"side":"long"}` or `"short"` |
| `/preview?side=long` | GET | Dry preview of the next rotation |
| `/book` | GET | Live Freqtrade weights + local book |
| `/book/seed` | POST | Record starting coins (`symbol`, `coins`, `cost_usd`) |
| `/health` | GET | No auth |

Auth is the same as IADSS: `token` in JSON body (preferred), `?token=`, or `X-Token` / `X-Webhook-Secret`.

## TradingView alerts

Four alerts on the **ratio chart**, once per bar close. Leave IADSS pair alerts on SOL/HYPE/TAO pointed at the original tracker.

Webhook URL (Long complete):

```
https://rotate.yourdomain.com/rotate-long
```

Message body:

```json
{"pair": "HYPE/SOL", "token": "YOUR_SECRET_TOKEN", "side": "long"}
```

Short complete → `/rotate-short` with `"side": "short"`.

## Setup (Kraken subaccount)

1. On Kraken, create a subaccount. Generate an API key with **Query** + **Create and modify orders** only (never withdrawals).
2. Transfer in only the rotation bankroll.
3. Clone and configure:

```bash
git clone https://github.com/ballzac81/IADSS-Rotation-Tracker.git
cd IADSS-Rotation-Tracker
cp .env.example .env
mkdir -p user_data/strategies
cp config.json user_data/
cp strategies/WebhookStrategy.py user_data/strategies/
```

4. Edit `user_data/config.json`:
   - Subaccount API key and secret
   - `"dry_run": true` until you have watched rotations
   - JWT secret, Freqtrade API password (must match `FREQTRADE_PASS` in `.env`)
   - `pair_whitelist`: `HYPE/USD`, `SOL/USD` only
5. Generate `SECRET_TOKEN` (`openssl rand -hex 24`) and put it in `.env`.

VPS:

```bash
docker compose up -d
```

Unraid (own Freqtrade, Cloudflare tunnel for HTTPS only):

```bash
docker compose -f docker-compose.selfhosted.yml up -d
```

`/boot/config/go`:

```bash
cd /mnt/user/appdata/IADSS-Rotation-Tracker && docker compose -f docker-compose.selfhosted.yml up -d
```

Cloudflare public hostnames (same tunnel as IADSS, **different containers**):

```
rotate.yourdomain.com        →  http://rotation-tracker:5000
rotate-trade.yourdomain.com  →  http://rotation-freqtrade:8080
```

Ports `5002` (tracker) and `8068` (Freqtrade UI) so they do not clash with IADSS.

## Seed existing holdings

This Freqtrade must have **open trades** on both pairs for sells to work. After a starter buy on each side in dry-run:

```bash
curl -X POST https://rotate.yourdomain.com/book/seed \
  -H "Content-Type: application/json" \
  -d '{"symbol":"HYPE","coins":12.5,"cost_usd":1000,"token":"YOUR_SECRET_TOKEN"}'
```

```bash
curl "https://rotate.yourdomain.com/preview?side=long&token=YOUR_SECRET_TOKEN"
```

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `ROTATE_BASE` | `HYPE` | Asset bought on Long |
| `ROTATE_QUOTE` | `SOL` | Asset bought on Short |
| `STAKE_CURRENCY` | `USD` | Freqtrade stake currency |
| `ROTATE_RATIO` | `0.5` | Fraction of the weaker side's **USD** sold |
| `MIN_STAKE` | `10` | Skip if the USD slice is below this |
| `FEE_BPS` | `0` | Optional haircut before the buy (16 = 0.16%) |
| `SETTLE_TIMEOUT` | `60` | Seconds to wait for the sell to credit free cash |

## Security

- Trade endpoints require `SECRET_TOKEN`
- Rate limit: 6/min on rotate, 30/min on early warning
- Never enable withdrawal permissions on the exchange key
- `.env` is gitignored
- One Freqtrade per Kraken account/subaccount

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Educational software, not financial advice. You are solely responsible for trades. Test in dry-run first. Never trade money you cannot afford to lose.
