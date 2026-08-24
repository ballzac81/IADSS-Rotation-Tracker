# IADSS Rotation Tracker

Pair rotator for the [IADSS Confluence Monitor](https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/). Forked in spirit from [IADSS-Signal-Tracker](https://github.com/ballzac81/IADSS-Signal-Tracker) — same webhook auth, Telegram, Freqtrade, Unraid sidecar — but it does **not** treat pairs as isolated bankrolls.

It rotates **USD value** between two spot assets you want to keep long.

> **Spot only. Never shorts.** A Long/Short label is the ratio direction, not a leveraged short.

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

Four alerts on the **ratio chart**, once per bar close.

Webhook URL (Long complete):

```
https://rotate.yourdomain.com/rotate-long
```

Message body:

```json
{"pair": "HYPE/SOL", "token": "YOUR_SECRET_TOKEN", "side": "long"}
```

Short complete → `/rotate-short` with `"side": "short"`.

## Deploy next to live IADSS (recommended)

Do **not** start a second Freqtrade against the same Kraken keys. Run this as a sidecar that talks to the Freqtrade you already have.

1. Add both assets to the **existing** Freqtrade whitelist:

```json
"pair_whitelist": ["SOL/USD", "HYPE/USD"]
```

2. Keep `"dry_run": true` until you have watched rotations on `/book` and Telegram.

3. Clone and configure:

```bash
git clone https://github.com/ballzac81/IADSS-Rotation-Tracker.git
cd IADSS-Rotation-Tracker
cp .env.example .env
```

Set `SECRET_TOKEN`, `FREQTRADE_PASS`, `DOCKER_NETWORK` (the network `freqtrade` and your Cloudflare tunnel already share).

4. Start:

```bash
docker compose -f docker-compose.sidecar.yml up -d
```

Unraid auto-start in `/boot/config/go`:

```bash
cd /mnt/user/appdata/IADSS-Rotation-Tracker && docker compose -f docker-compose.sidecar.yml up -d
```

5. Cloudflare tunnel public hostname:

```
rotate.yourdomain.com  →  http://rotation-tracker:5000
```

Host port is `5002` so it does not clash with IADSS (`5000`) or PTOS (`5001`).

## Standalone (no existing Freqtrade)

Only if this is the only bot:

```bash
mkdir -p user_data/strategies
cp config.json user_data/
cp strategies/WebhookStrategy.py user_data/strategies/
# edit user_data/config.json placeholders
docker compose up -d
```

## Seed existing holdings

Freqtrade must already have **open trades** on both pairs for sells to work (same rule as IADSS). If you already hold coins, force-enter or buy a starter size on each pair in dry-run, then:

```bash
curl -X POST https://rotate.yourdomain.com/book/seed \
  -H "Content-Type: application/json" \
  -d '{"symbol":"HYPE","coins":12.5,"cost_usd":1000,"token":"YOUR_SECRET_TOKEN"}'
```

Check what the next Long would do:

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

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Educational software, not financial advice. You are solely responsible for trades. Test in dry-run first. Never trade money you cannot afford to lose.
