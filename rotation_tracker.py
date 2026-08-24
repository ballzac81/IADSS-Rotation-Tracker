#!/usr/bin/env python3
"""IADSS Rotation Tracker — Hyperliquid spot rotator.

TradingView fires Long/Short on a ratio chart (e.g. HYPEUSD/SOLUSD).
This server rotates USD value between two spot assets on a dedicated
Hyperliquid subaccount:

  Long  = base stronger  → sell ROTATE_RATIO of quote USD → buy base
  Short = quote stronger → sell ROTATE_RATIO of base USD  → buy quote

Never shorts. Never talks to Kraken / Freqtrade / IADSS.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

PAIR_RE = re.compile(r"^[A-Z0-9]+/[A-Z0-9]+$")
SIDE_RE = re.compile(r"^(long|short)$", re.I)

SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

ROTATE_BASE = os.environ.get("ROTATE_BASE", "HYPE").upper()
ROTATE_QUOTE = os.environ.get("ROTATE_QUOTE", "SOL").upper()
STAKE_CURRENCY = os.environ.get("STAKE_CURRENCY", "USDC").upper()
ROTATE_RATIO = float(os.environ.get("ROTATE_RATIO", "0.5"))
MIN_STAKE = float(os.environ.get("MIN_STAKE", "10.0"))
FEE_BPS = float(os.environ.get("FEE_BPS", "0"))
SLIPPAGE = float(os.environ.get("SLIPPAGE", "0.01"))
SETTLE_TIMEOUT = float(os.environ.get("SETTLE_TIMEOUT", "30"))
SETTLE_POLL = float(os.environ.get("SETTLE_POLL", "1.5"))
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() in ("1", "true", "yes", "on")

HL_AGENT_PRIVATE_KEY = (os.environ.get("HL_AGENT_PRIVATE_KEY") or os.environ.get("HL_SECRET_KEY") or "").strip()
HL_ACCOUNT_ADDRESS = (os.environ.get("HL_ACCOUNT_ADDRESS") or "").strip()
HL_SUBACCOUNT_ADDRESS = (os.environ.get("HL_SUBACCOUNT_ADDRESS") or "").strip()
HL_NETWORK = (os.environ.get("HL_NETWORK") or "mainnet").strip().lower()
HL_SPOT_BASE = os.environ.get("HL_SPOT_BASE", "").strip()
HL_SPOT_QUOTE = os.environ.get("HL_SPOT_QUOTE", "").strip()

BOOK_FILE = os.environ.get("BOOK_FILE", "/data/rotation_book.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

if not SECRET_TOKEN:
    logger.warning("SECRET_TOKEN is not set — endpoints are UNAUTHENTICATED")
if DRY_RUN:
    logger.warning("DRY_RUN=true — orders will NOT be sent to Hyperliquid")

app = Flask(__name__)
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")
_book_lock = threading.Lock()
_hl_lock = threading.Lock()
_hl = None


def ratio_pair() -> str:
    return f"{ROTATE_BASE}/{ROTATE_QUOTE}"


def trading_address() -> str:
    return HL_SUBACCOUNT_ADDRESS or HL_ACCOUNT_ADDRESS


def hl_base_url() -> str:
    from hyperliquid.utils import constants

    return constants.TESTNET_API_URL if HL_NETWORK == "testnet" else constants.MAINNET_API_URL


def telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg},
            timeout=5,
        )
    except Exception as e:
        logger.warning("Telegram failed: %s", e)


def _init_hl():
    if not HL_AGENT_PRIVATE_KEY or not HL_ACCOUNT_ADDRESS:
        raise RuntimeError("HL_AGENT_PRIVATE_KEY and HL_ACCOUNT_ADDRESS are required")
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    key = HL_AGENT_PRIVATE_KEY
    if not key.startswith("0x"):
        key = "0x" + key
    wallet = Account.from_key(key)
    account = HL_ACCOUNT_ADDRESS
    if not account.startswith("0x"):
        account = "0x" + account
    vault = HL_SUBACCOUNT_ADDRESS or None
    if vault and not vault.startswith("0x"):
        vault = "0x" + vault
    url = hl_base_url()
    info = Info(url, skip_ws=True)
    exchange = Exchange(
        wallet,
        url,
        account_address=account,
        vault_address=vault,
    )
    logger.info(
        "Hyperliquid ready network=%s agent=%s account=%s vault=%s",
        HL_NETWORK,
        wallet.address,
        account,
        vault or "none",
    )
    return {"info": info, "exchange": exchange, "account": account, "vault": vault}


def hl():
    global _hl
    with _hl_lock:
        if _hl is None:
            _hl = _init_hl()
        return _hl


def query_address() -> str:
    addr = trading_address()
    if not addr:
        raise RuntimeError("HL_SUBACCOUNT_ADDRESS or HL_ACCOUNT_ADDRESS is required")
    return addr if addr.startswith("0x") else "0x" + addr


def _token_aliases(symbol: str) -> list[str]:
    s = symbol.upper()
    names = [s, f"U{s}", f"{s}/USDC", f"U{s}/USDC"]
    extra = os.environ.get(f"HL_SPOT_{s}", "").strip()
    if extra:
        names.insert(0, extra)
    return names


def spot_name(symbol: str) -> str:
    if symbol.upper() == ROTATE_BASE and HL_SPOT_BASE:
        return HL_SPOT_BASE
    if symbol.upper() == ROTATE_QUOTE and HL_SPOT_QUOTE:
        return HL_SPOT_QUOTE
    info = hl()["info"]
    for candidate in _token_aliases(symbol):
        if candidate in info.name_to_coin:
            return candidate
    raise RuntimeError(
        f"could not resolve Hyperliquid spot market for {symbol}. "
        f"Set HL_SPOT_{symbol.upper()} (e.g. USOL/USDC)"
    )


def mid_px(symbol: str) -> float:
    info = hl()["info"]
    name = spot_name(symbol)
    coin = info.name_to_coin[name]
    mids = info.all_mids()
    px = float(mids.get(coin) or mids.get(name) or 0)
    if px <= 0:
        raise RuntimeError(f"no mid price for {symbol} ({name})")
    return px


def sz_decimals(symbol: str) -> int:
    info = hl()["info"]
    name = spot_name(symbol)
    coin = info.name_to_coin[name]
    asset = info.coin_to_asset[coin]
    return int(info.asset_to_sz_decimals[asset])


def round_sz(sz: float, decimals: int, up: bool = False) -> float:
    factor = 10 ** decimals
    if up:
        return math.ceil(sz * factor - 1e-12) / factor
    return math.floor(sz * factor + 1e-12) / factor


def spot_balances() -> dict[str, dict]:
    state = hl()["info"].spot_user_state(query_address())
    out = {}
    for row in state.get("balances") or []:
        coin = str(row.get("coin") or "").upper()
        total = float(row.get("total") or 0)
        hold = float(row.get("hold") or 0)
        out[coin] = {
            "total": total,
            "hold": hold,
            "free": max(0.0, total - hold),
            "entry_ntl": float(row.get("entryNtl") or 0),
            "raw": row,
        }
    return out


def balance_of(symbol: str) -> dict:
    bals = spot_balances()
    for key in _token_aliases(symbol):
        bare = key.split("/")[0].upper()
        if bare in bals:
            return bals[bare]
    return {"total": 0.0, "hold": 0.0, "free": 0.0, "entry_ntl": 0.0}


def position(symbol: str) -> tuple[float, float]:
    """(free coins, usd value at mid)."""
    bal = balance_of(symbol)
    coins = float(bal["free"])
    if coins <= 0:
        return 0.0, 0.0
    return coins, coins * mid_px(symbol)


def usdc_free() -> float:
    return float(balance_of(STAKE_CURRENCY)["free"])


def wait_until(predicate, timeout: float, label: str) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception as e:
            logger.warning("Settle poll (%s) error: %s", label, e)
        time.sleep(SETTLE_POLL)
    return False


def parse_order_result(result: dict) -> dict:
    if not isinstance(result, dict):
        return {"raw": result}
    if result.get("status") != "ok":
        raise RuntimeError(f"Hyperliquid order failed: {result}")
    statuses = (((result.get("response") or {}).get("data") or {}).get("statuses")) or []
    if not statuses:
        return {"raw": result}
    status = statuses[0]
    if "error" in status:
        raise RuntimeError(f"Hyperliquid order error: {status['error']}")
    return {"status": status, "raw": result}


def market_spot(symbol: str, is_buy: bool, sz: float) -> dict:
    name = spot_name(symbol)
    sz = round_sz(sz, sz_decimals(symbol), up=False)
    if sz <= 0:
        raise RuntimeError(f"rounded size is 0 for {symbol}")
    logger.info("HL %s %s sz=%s name=%s dry_run=%s", "BUY" if is_buy else "SELL", symbol, sz, name, DRY_RUN)
    if DRY_RUN:
        px = mid_px(symbol)
        return {"dry_run": True, "name": name, "sz": sz, "px": px, "usd": sz * px}
    result = hl()["exchange"].market_open(name, is_buy, sz, None, SLIPPAGE)
    parsed = parse_order_result(result)
    parsed.update({"name": name, "sz": sz})
    return parsed


def sell_usd(symbol: str, target_usd: float) -> dict:
    coins, usd = position(symbol)
    if coins <= 0 or usd <= 0:
        raise RuntimeError(f"no free {symbol} spot to sell")
    px = mid_px(symbol)
    want_coins = min(coins, target_usd / px)
    if want_coins / coins > 0.97:
        want_coins = coins
    sold = market_spot(symbol, False, want_coins)
    sold_coins = float(sold.get("sz") or want_coins)
    sold_usd = sold_coins * px
    return {"sold_coins": sold_coins, "sold_usd": sold_usd, "px": px, "fill": sold}


def buy_usd(symbol: str, stake: float) -> dict:
    if stake < MIN_STAKE:
        raise RuntimeError(f"buy stake ${stake:.2f} below ${MIN_STAKE:.0f} minimum")
    px = mid_px(symbol)
    cash = usdc_free()
    use = min(stake, cash) if not DRY_RUN else stake
    if use < MIN_STAKE:
        raise RuntimeError(f"free {STAKE_CURRENCY} ${cash:.2f} too low to buy {symbol}")
    sz = use / (px * (1 + SLIPPAGE))
    filled = market_spot(symbol, True, sz)
    bought_usd = float(filled.get("sz") or sz) * px
    return {"bought_usd": bought_usd, "sz": filled.get("sz"), "px": px, "fill": filled}


def _empty_book() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "venue": "hyperliquid",
        "base": ROTATE_BASE,
        "quote": ROTATE_QUOTE,
        "stake": STAKE_CURRENCY,
        "ratio": ROTATE_RATIO,
        "assets": {
            ROTATE_BASE: {"coins": 0.0, "cost_usd": 0.0},
            ROTATE_QUOTE: {"coins": 0.0, "cost_usd": 0.0},
        },
        "history": [],
        "created": now,
        "updated": now,
    }


def _load_book() -> dict:
    if os.path.exists(BOOK_FILE):
        try:
            with open(BOOK_FILE) as f:
                data = json.load(f)
            data.setdefault("assets", {})
            data["assets"].setdefault(ROTATE_BASE, {"coins": 0.0, "cost_usd": 0.0})
            data["assets"].setdefault(ROTATE_QUOTE, {"coins": 0.0, "cost_usd": 0.0})
            data.setdefault("history", [])
            data["venue"] = "hyperliquid"
            return data
        except Exception as e:
            logger.error("Failed to load book: %s", e)
    return _empty_book()


def _save_book(book: dict):
    dirpath = os.path.dirname(BOOK_FILE) or "."
    os.makedirs(dirpath, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirpath, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(book, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, BOOK_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def apply_book_rotation(side: str, sell_symbol: str, buy_symbol: str, sold_coins: float, sold_usd: float, bought_usd: float):
    with _book_lock:
        book = _load_book()
        sell = book["assets"].setdefault(sell_symbol, {"coins": 0.0, "cost_usd": 0.0})
        buy = book["assets"].setdefault(buy_symbol, {"coins": 0.0, "cost_usd": 0.0})
        if sell["coins"] > 0:
            cost_out = sell["cost_usd"] * (sold_coins / sell["coins"])
        else:
            cost_out = sold_usd
        sell["coins"] = max(0.0, sell["coins"] - sold_coins)
        sell["cost_usd"] = max(0.0, sell["cost_usd"] - cost_out)
        buy["cost_usd"] += bought_usd
        book["history"].append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "side": side,
                "sell": sell_symbol,
                "buy": buy_symbol,
                "sold_coins": sold_coins,
                "sold_usd": round(sold_usd, 2),
                "bought_usd": round(bought_usd, 2),
                "dry_run": DRY_RUN,
            }
        )
        book["history"] = book["history"][-200:]
        book["updated"] = datetime.now(timezone.utc).isoformat()
        _save_book(book)
        return json.loads(json.dumps(book))


def seed_asset(symbol: str, coins: float, cost_usd: float) -> dict:
    if coins < 0 or cost_usd < 0:
        raise ValueError("coins and cost_usd must be >= 0")
    with _book_lock:
        book = _load_book()
        book["assets"][symbol] = {"coins": coins, "cost_usd": cost_usd}
        book["updated"] = datetime.now(timezone.utc).isoformat()
        _save_book(book)
        return dict(book["assets"][symbol])


def execute_rotate(side: str) -> dict:
    side = side.lower()
    if side not in ("long", "short"):
        raise ValueError("side must be long or short")

    if side == "long":
        sell_symbol, buy_symbol = ROTATE_QUOTE, ROTATE_BASE
    else:
        sell_symbol, buy_symbol = ROTATE_BASE, ROTATE_QUOTE

    coins, usd = position(sell_symbol)
    target = usd * ROTATE_RATIO
    preview = {
        "side": side,
        "venue": "hyperliquid",
        "dry_run": DRY_RUN,
        "ratio_pair": ratio_pair(),
        "sell": sell_symbol,
        "buy": buy_symbol,
        "sell_usd": round(usd, 2),
        "sell_coins": coins,
        "target_usd": round(target, 2),
        "ratio": ROTATE_RATIO,
        "usdc_free": round(usdc_free(), 2),
    }

    if usd <= 0 or coins <= 0:
        msg = f"IADSS ROTATE skipped — {side.upper()} {ratio_pair()}\nNo free {sell_symbol} spot on Hyperliquid sub"
        logger.warning(msg)
        telegram(msg)
        return {"status": "skipped", "reason": f"no free {sell_symbol}", **preview}

    if target < MIN_STAKE:
        msg = (
            f"IADSS ROTATE skipped — {side.upper()} {ratio_pair()}\n"
            f"{int(ROTATE_RATIO*100)}% of {sell_symbol} is ${target:.2f} (min ${MIN_STAKE:.0f})"
        )
        logger.warning(msg)
        telegram(msg)
        return {"status": "skipped", "reason": "below minimum", **preview}

    mode = "DRY RUN " if DRY_RUN else ""
    telegram(
        f"IADSS ROTATE {mode}{side.upper()} — {ratio_pair()} (Hyperliquid spot)\n"
        f"Selling ${target:.2f} of {sell_symbol} ({int(ROTATE_RATIO*100)}%) → buying {buy_symbol}"
    )

    sell_info = sell_usd(sell_symbol, target)
    sold_usd = float(sell_info["sold_usd"])
    fee = sold_usd * (FEE_BPS / 10_000.0)
    buy_stake = max(0.0, sold_usd - fee)

    if not DRY_RUN:
        wait_until(lambda: usdc_free() >= min(buy_stake * 0.95, MIN_STAKE), SETTLE_TIMEOUT, "usdc after sell")

    buy_info = None
    buy_error = None
    try:
        buy_info = buy_usd(buy_symbol, buy_stake)
        bought_usd = float(buy_info["bought_usd"])
    except Exception as e:
        buy_error = str(e)
        bought_usd = 0.0
        logger.error("Buy failed after sell: %s", e)

    apply_book_rotation(side, sell_symbol, buy_symbol, float(sell_info["sold_coins"]), sold_usd, bought_usd)

    if buy_error:
        msg = (
            f"IADSS ROTATE PARTIAL — {side.upper()} {ratio_pair()}\n"
            f"SOLD {sell_info['sold_coins']:.6f} {sell_symbol} (~${sold_usd:.2f})\n"
            f"BUY {buy_symbol} FAILED: {buy_error}\n"
            f"{STAKE_CURRENCY} is sitting in the HL sub — buy manually or retry"
        )
        telegram(msg)
        return {
            "status": "partial",
            "error": buy_error,
            **preview,
            "sold_usd": round(sold_usd, 2),
            "sold_coins": sell_info["sold_coins"],
            "bought_usd": 0,
        }

    msg = (
        f"IADSS ROTATE {mode}executed — {side.upper()} {ratio_pair()}\n"
        f"Sold: {sell_info['sold_coins']:.6f} {sell_symbol}  ${sold_usd:.2f}\n"
        f"Bought: {buy_symbol}  ${bought_usd:.2f}\n"
        f"Venue: Hyperliquid spot{' (dry run)' if DRY_RUN else ''}"
    )
    telegram(msg)
    logger.info(msg.replace("\n", " | "))
    return {
        "status": "dry_run" if DRY_RUN else "rotated",
        **preview,
        "sold_usd": round(sold_usd, 2),
        "sold_coins": sell_info["sold_coins"],
        "bought_usd": round(bought_usd, 2),
    }


def preview_rotate(side: str) -> dict:
    side = side.lower()
    sell_symbol = ROTATE_QUOTE if side == "long" else ROTATE_BASE
    buy_symbol = ROTATE_BASE if side == "long" else ROTATE_QUOTE
    coins, usd = position(sell_symbol)
    target = usd * ROTATE_RATIO
    return {
        "side": side,
        "venue": "hyperliquid",
        "dry_run": DRY_RUN,
        "ratio_pair": ratio_pair(),
        "sell": sell_symbol,
        "buy": buy_symbol,
        "position_usd": round(usd, 2),
        "position_coins": coins,
        "target_usd": round(target, 2),
        "would_skip": usd <= 0 or target < MIN_STAKE,
        "usdc_free": round(usdc_free(), 2),
        "mids": {ROTATE_BASE: mid_px(ROTATE_BASE), ROTATE_QUOTE: mid_px(ROTATE_QUOTE)},
    }


def live_snapshot() -> dict:
    base_coins, base_usd = position(ROTATE_BASE)
    quote_coins, quote_usd = position(ROTATE_QUOTE)
    total = base_usd + quote_usd
    cash = usdc_free()
    return {
        ROTATE_BASE: {
            "coins": base_coins,
            "usd": round(base_usd, 2),
            "px": mid_px(ROTATE_BASE),
            "weight_pct": round(base_usd / total * 100, 2) if total else 0,
            "spot": spot_name(ROTATE_BASE),
        },
        ROTATE_QUOTE: {
            "coins": quote_coins,
            "usd": round(quote_usd, 2),
            "px": mid_px(ROTATE_QUOTE),
            "weight_pct": round(quote_usd / total * 100, 2) if total else 0,
            "spot": spot_name(ROTATE_QUOTE),
        },
        "total_usd": round(total, 2),
        "usdc_free": round(cash, 2),
        "subaccount": query_address(),
    }


def require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        data = request.get_json(silent=True) or {}
        token = (
            request.args.get("token")
            or request.headers.get("X-Token")
            or request.headers.get("X-Webhook-Secret")
            or data.get("token")
            or ""
        )
        if SECRET_TOKEN and token != SECRET_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)

    return decorated


def parse_side(default: str | None = None) -> str:
    data = request.get_json(silent=True) or {}
    side = (data.get("side") or request.args.get("side") or default or "").lower()
    if not SIDE_RE.match(side or ""):
        raise ValueError("side must be long or short")
    return side


@app.route("/confirm-long", methods=["POST"])
@limiter.limit("30 per minute")
@require_token
def confirm_long():
    logger.info("LONG early warning: %s", ratio_pair())
    telegram(
        f"IADSS ROTATE Early Warning — LONG {ratio_pair()}\n"
        f"{ROTATE_BASE} looking stronger — waiting for sequence complete"
    )
    return jsonify({"status": "ok", "message": "early_warning", "side": "long"}), 200


@app.route("/confirm-short", methods=["POST"])
@limiter.limit("30 per minute")
@require_token
def confirm_short():
    logger.info("SHORT early warning: %s", ratio_pair())
    telegram(
        f"IADSS ROTATE Early Warning — SHORT {ratio_pair()}\n"
        f"{ROTATE_QUOTE} looking stronger — waiting for sequence complete"
    )
    return jsonify({"status": "ok", "message": "early_warning", "side": "short"}), 200


@app.route("/rotate-long", methods=["POST"])
@limiter.limit("6 per minute")
@require_token
def rotate_long():
    try:
        result = execute_rotate("long")
        code = 200 if result.get("status") in ("rotated", "skipped", "dry_run") else 207
        return jsonify(result), code
    except Exception as e:
        logger.error("rotate-long failed: %s", e)
        telegram(f"IADSS ROTATE FAILED — LONG {ratio_pair()}\n{e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/rotate-short", methods=["POST"])
@limiter.limit("6 per minute")
@require_token
def rotate_short():
    try:
        result = execute_rotate("short")
        code = 200 if result.get("status") in ("rotated", "skipped", "dry_run") else 207
        return jsonify(result), code
    except Exception as e:
        logger.error("rotate-short failed: %s", e)
        telegram(f"IADSS ROTATE FAILED — SHORT {ratio_pair()}\n{e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/rotate", methods=["POST"])
@limiter.limit("6 per minute")
@require_token
def rotate():
    try:
        side = parse_side()
        result = execute_rotate(side)
        code = 200 if result.get("status") in ("rotated", "skipped", "dry_run") else 207
        return jsonify(result), code
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("rotate failed: %s", e)
        telegram(f"IADSS ROTATE FAILED — {ratio_pair()}\n{e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/preview", methods=["GET"])
@limiter.limit("60 per minute")
@require_token
def preview():
    try:
        side = (request.args.get("side") or "long").lower()
        if side not in ("long", "short"):
            return jsonify({"error": "side must be long or short"}), 400
        return jsonify({"status": "ok", **preview_rotate(side)})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/book", methods=["GET"])
@limiter.limit("60 per minute")
@require_token
def book_view():
    try:
        with _book_lock:
            book = _load_book()
        return jsonify(
            {
                "status": "ok",
                "venue": "hyperliquid",
                "dry_run": DRY_RUN,
                "ratio_pair": ratio_pair(),
                "live": live_snapshot(),
                "book": book,
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/book/seed", methods=["POST"])
@limiter.limit("20 per minute")
@require_token
def book_seed():
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").upper()
    if symbol not in (ROTATE_BASE, ROTATE_QUOTE):
        return jsonify({"error": f"symbol must be {ROTATE_BASE} or {ROTATE_QUOTE}"}), 400
    try:
        coins = float(data.get("coins"))
        cost_usd = float(data.get("cost_usd"))
    except (TypeError, ValueError):
        return jsonify({"error": "coins and cost_usd must be numbers"}), 400
    try:
        entry = seed_asset(symbol, coins, cost_usd)
        telegram(f"IADSS ROTATE SEED\n{symbol}: {coins} coins  cost ${cost_usd:.2f}")
        return jsonify({"status": "ok", "symbol": symbol, **entry})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/book/sync", methods=["POST"])
@limiter.limit("20 per minute")
@require_token
def book_sync():
    """Pull live Hyperliquid spot balances into the local book (cost from entryNtl)."""
    try:
        seeded = {}
        for symbol in (ROTATE_BASE, ROTATE_QUOTE):
            bal = balance_of(symbol)
            coins = float(bal["total"])
            cost = float(bal["entry_ntl"] or 0)
            if cost <= 0 and coins > 0:
                cost = coins * mid_px(symbol)
            seeded[symbol] = seed_asset(symbol, coins, cost)
        telegram(f"IADSS ROTATE SYNC from Hyperliquid spot\n{json.dumps(seeded)}")
        return jsonify({"status": "ok", "assets": seeded, "live": live_snapshot()})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "service": "iadss-rotation-tracker",
            "venue": "hyperliquid",
            "network": HL_NETWORK,
            "ratio_pair": ratio_pair(),
            "ratio": ROTATE_RATIO,
            "dry_run": DRY_RUN,
            "subaccount_configured": bool(HL_SUBACCOUNT_ADDRESS),
        }
    )


if __name__ == "__main__":
    logger.info(
        "IADSS Rotation Tracker starting — %s ratio=%.2f venue=hyperliquid dry_run=%s",
        ratio_pair(),
        ROTATE_RATIO,
        DRY_RUN,
    )
    app.run(host="0.0.0.0", port=5000)
