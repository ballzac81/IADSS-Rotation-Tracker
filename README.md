# IADSS Rotation Tracker

Pair rotator for the [IADSS Confluence Monitor](https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/). Same webhook / Telegram / Unraid style as [IADSS-Signal-Tracker](https://github.com/ballzac81/IADSS-Signal-Tracker), but it trades **Hyperliquid spot** on its **own subaccount**.

Leave Kraken IADSS (SOL / HYPE / TAO) and PTOS (HL perps) exactly as they are.

> **Spot only. Never shorts.** Long/Short is the ratio direction, not a leveraged short.

## How a rotation works

Chart the ratio (example: `HYPEUSD/SOLUSD` on TradingView). When IADSS completes a sequence:

| Signal | Meaning | Action (default 50%) |
|--------|---------|----------------------|
| **Long** | Base (HYPE) is stronger | Sell 50% of **SOL USD value** → buy HYPE with the USDC |
| **Short** | Quote (SOL) is stronger | Sell 50% of **HYPE USD value** → buy SOL with the USDC |

Sizing is always **USD of the weaker side**, not token count. You stay long both.

## Keep the books separate

| Sleeve | Where | What |
|--------|--------|------|
| IADSS independent | Kraken business + existing Freqtrade | SOL / HYPE / TAO pair alerts |
| PTOS | Existing Hyperliquid account/sub | Perps — do not reuse |
| **HYPE/SOL rotation** | **New HL subaccount** `HYPE-SOL-rotate` | This bot, spot only |

## Hyperliquid setup (same idea as PTOS)

1. [app.hyperliquid.xyz/subAccounts](https://app.hyperliquid.xyz/subAccounts) → create `HYPE-SOL-rotate`.
2. Send **spot** HYPE and SOL (or USDC) into that sub. Use spot send, not perps USDC.
3. [app.hyperliquid.xyz/API](https://app.hyperliquid.xyz/API) → generate an **agent wallet** authorized for that sub. Trading only, no withdraw.
4. In Trade UI, switch to the sub, open **Spot**, confirm `HYPE/USDC` and `SOL/USDC` (SOL may show as USOL).

`.env`:

```
HL_AGENT_PRIVATE_KEY=          # agent private key
HL_ACCOUNT_ADDRESS=            # master wallet you connect in the UI
HL_SUBACCOUNT_ADDRESS=         # 0x… of HYPE-SOL-rotate
DRY_RUN=true
```

SOL on HL spot is often `USOL`. If `/preview` cannot resolve it:

```
HL_SPOT_QUOTE=USOL/USDC
HL_SPOT_BASE=HYPE/USDC
```

## Endpoints

| Endpoint | Method | Action |
|----------|--------|--------|
| `/confirm-long` | POST | Telegram early warning only |
| `/confirm-short` | POST | Telegram early warning only |
| `/rotate-long` | POST | Execute Long rotation |
| `/rotate-short` | POST | Execute Short rotation |
| `/rotate` | POST | Body `{"side":"long"}` or `"short"` |
| `/preview?side=long` | GET | Dry preview of the next rotation |
| `/book` | GET | Live HL spot weights + local book |
| `/book/sync` | POST | Seed book from live HL balances |
| `/book/seed` | POST | Manual seed (`symbol`, `coins`, `cost_usd`) |
| `/health` | GET | No auth |

Auth: `token` in JSON body (preferred), `?token=`, or `X-Token` / `X-Webhook-Secret`.

## TradingView alerts

Four alerts on the **ratio chart**, once per bar close. IADSS pair alerts stay pointed at the original Kraken tracker.

```
https://rotate.yourdomain.com/rotate-long
```

```json
{"pair": "HYPE/SOL", "token": "YOUR_SECRET_TOKEN", "side": "long"}
```

Short complete → `/rotate-short` with `"side": "short"`.

## Deploy


```bash
git clone https://github.com/ballzac81/IADSS-Rotation-Tracker.git
cd IADSS-Rotation-Tracker
cp .env.example .env
# fill SECRET_TOKEN, HL_* keys, TELEGRAM_*, keep DRY_RUN=true
docker compose up -d
```

Unraid (Cloudflare tunnel for HTTPS):

```bash
docker compose -f docker-compose.selfhosted.yml up -d
```

`/boot/config/go`:

```bash
cd /mnt/user/appdata/IADSS-Rotation-Tracker && docker compose -f docker-compose.selfhosted.yml up -d
```

### Portainer (Unraid)

1. Portainer → **Stacks** → **Add stack**
2. Build method: **Repository**
3. Repository URL: `https://github.com/ballzac81/IADSS-Rotation-Tracker.git`
4. Compose path: `docker-compose.portainer.yml`
5. **Environment variables** in the UI (same names as `.env.example`):

| Required | Example |
|----------|---------|
| `DOCKER_NETWORK` | the tunnel network IADSS already uses |
| `SECRET_TOKEN` | `openssl rand -hex 24` |
| `HL_AGENT_PRIVATE_KEY` | agent wallet key |
| `HL_ACCOUNT_ADDRESS` | master `0x…` |
| `HL_SUBACCOUNT_ADDRESS` | `HYPE-SOL-rotate` `0x…` |
| `DRY_RUN` | `true` until you have watched `/preview` |

Optional: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `HL_SPOT_QUOTE=USOL/USDC`

6. Deploy the stack. Portainer builds the image from the Dockerfile.

Cloudflare (same tunnel as IADSS, new hostname):

```
rotate.yourdomain.com  →  http://rotation-tracker:5000
```

Host port `5002` so it does not clash with IADSS (`5000`) or PTOS (`5001`). Data lives in the named volume `rotation-data` (Portainer → Volumes). To bind it to Unraid appdata instead, change the volume to `/mnt/user/appdata/IADSS-Rotation-Tracker/data:/data`.

Keep `DRY_RUN=true` until `/preview` and Telegram look right. Then set `DRY_RUN=false` and recreate the container.

Sync the book from live HL spot:

```bash
curl -X POST https://rotate.yourdomain.com/book/sync \
  -H "Content-Type: application/json" \
  -d '{"token":"YOUR_SECRET_TOKEN"}'
```

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `DRY_RUN` | `true` | If true, sizes and Telegram fire but no HL orders |
| `ROTATE_BASE` | `HYPE` | Asset bought on Long |
| `ROTATE_QUOTE` | `SOL` | Asset bought on Short |
| `STAKE_CURRENCY` | `USDC` | HL spot quote |
| `ROTATE_RATIO` | `0.5` | Fraction of the weaker side's **USD** sold |
| `MIN_STAKE` | `10` | Skip if the USD slice is below this |
| `SLIPPAGE` | `0.01` | IOC market slippage (1%) |
| `FEE_BPS` | `0` | Optional haircut before the buy |

## Security

- Trade endpoints require `SECRET_TOKEN`
- Rate limit: 6/min on rotate, 30/min on early warning
- Use an **agent wallet** (cannot withdraw)
- `.env` is gitignored
- Do not reuse the PTOS subaccount (unified USDC is one pot on that sub)

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Educational software, not financial advice. You are solely responsible for trades. Test with `DRY_RUN=true` first. Never trade money you cannot afford to lose.
