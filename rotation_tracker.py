#!/usr/bin/env python3
"""IADSS Rotation Tracker — pair rotator for two spot assets.

Based on IADSS-Signal-Tracker. TradingView fires Long/Short on a ratio chart
(e.g. HYPEUSD/SOLUSD). This server rotates USD value between the two assets:

  Long  = base stronger  → sell ROTATE_RATIO of quote USD → buy base
  Short = quote stronger → sell ROTATE_RATIO of base USD  → buy quote

Never shorts. Both assets stay long; only the weighting changes.

Default: HYPE/SOL, 50% of the weaker side's current USD value.
"""

from __future__ import annotations

import json
import logging
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


def valid_pair(pair: str) -> bool:
    return bool(PAIR_RE.match(pair))


SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "")
FREQTRADE_API = os.environ.get("FREQTRADE_API", "http://freqtrade:8080/api/v1")
FREQTRADE_USER = os.environ.get("FREQTRADE_USER", "admin")
FREQTRADE_PASS = os.environ.get("FREQTRADE_PASS", "")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

ROTATE_BASE = os.environ.get("ROTATE_BASE", "HYPE").upper()
ROTATE_QUOTE = os.environ.get("ROTATE_QUOTE", "SOL").upper()
STAKE_CURRENCY = os.environ.get("STAKE_CURRENCY", "USD").upper()
ROTATE_RATIO = float(os.environ.get("ROTATE_RATIO", "0.5"))
MIN_STAKE = float(os.environ.get("MIN_STAKE", "10.0"))
FEE_BPS = float(os.environ.get("FEE_BPS", "0"))
SETTLE_TIMEOUT = float(os.environ.get("SETTLE_TIMEOUT", "60"))
SETTLE_POLL = float(os.environ.get("SETTLE_POLL", "2.0"))

API_RETRIES = int(os.environ.get("API_RETRIES", "3"))
API_RETRY_DELAY = float(os.environ.get("API_RETRY_DELAY", "5.0"))
API_TIMEOUT = int(os.environ.get("API_TIMEOUT", "20"))
BOOK_FILE = os.environ.get("BOOK_FILE", "/data/rotation_book.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

if not SECRET_TOKEN:
    logger.warning("SECRET_TOKEN is not set — endpoints are UNAUTHENTICATED")

app = Flask(__name__)
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")
_book_lock = threading.Lock()


def base_pair() -> str:
    return f"{ROTATE_BASE}/{STAKE_CURRENCY}"


def quote_pair() -> str:
    return f"{ROTATE_QUOTE}/{STAKE_CURRENCY}"


def ratio_pair() -> str:
    return f"{ROTATE_BASE}/{ROTATE_QUOTE}"


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


def _ft_request(method: str, endpoint: str, **kwargs) -> dict | list:
    url = f"{FREQTRADE_API}/{endpoint.lstrip('/')}"
    auth = (FREQTRADE_USER, FREQTRADE_PASS)
    last_error = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = requests.request(method, url, auth=auth, timeout=API_TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_error = e
            logger.warning("Freqtrade API attempt %d/%d failed: %s", attempt, API_RETRIES, e)
            if attempt < API_RETRIES:
                time.sleep(API_RETRY_DELAY)
    raise RuntimeError(f"Freqtrade API failed after {API_RETRIES} attempts: {last_error}")


def _empty_book() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
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


def get_open_trades(pair: str) -> list[dict]:
    data = _ft_request("GET", "/status")
    if not isinstance(data, list):
        return []
    trades = [t for t in data if t.get("pair") == pair and t.get("is_open")]
    return sorted(trades, key=lambda t: t.get("open_date", ""), reverse=True)


def trade_rate(trade: dict) -> float:
    rate = trade.get("current_rate")
    if rate:
        return float(rate)
    return float(trade.get("open_rate") or 0)


def position_usd(pair: str) -> tuple[list[dict], float, float]:
    trades = get_open_trades(pair)
    coins = 0.0
    usd = 0.0
    for t in trades:
        amt = float(t.get("amount") or 0)
        rate = trade_rate(t)
        coins += amt
        usd += amt * rate
    return trades, coins, usd


def get_available_capital() -> float:
    data = _ft_request("GET", "/balance")
    if not isinstance(data, dict):
        return 0.0
    return float(data.get("available_capital", data.get("total", 0)) or 0)


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


def sell_usd(pair: str, target_usd: float) -> dict:
    """Sell newest-first until ~target_usd of the pair is exited. Market orders."""
    trades, coins, usd = position_usd(pair)
    if not trades or usd <= 0:
        raise RuntimeError(f"no open {pair} trade to sell")

    remaining = min(target_usd, usd)
    sold_usd = 0.0
    sold_coins = 0.0
    fills = []

    for trade in trades:
        if remaining < MIN_STAKE * 0.5:
            break
        trade_id = str(trade["trade_id"])
        amount = float(trade["amount"])
        rate = trade_rate(trade)
        if rate <= 0 or amount <= 0:
            continue
        trade_usd = amount * rate
        this_usd = min(remaining, trade_usd)
        this_coins = this_usd / rate
        if this_coins > amount * 0.999:
            this_coins = amount
            this_usd = amount * rate

        logger.info("FORCESELL %s trade_id=%s coins=%.8f (~$%.2f)", pair, trade_id, this_coins, this_usd)
        _ft_request(
            "POST",
            "/forcesell",
            json={"tradeid": trade_id, "ordertype": "market", "amount": this_coins},
        )

        prev_amount = amount
        settled = wait_until(
            lambda: (
                (get_open_trades(pair) == [] and True)
                or any(
                    str(t["trade_id"]) == trade_id and float(t["amount"]) < prev_amount * 0.999
                    for t in get_open_trades(pair)
                )
                or all(str(t["trade_id"]) != trade_id for t in get_open_trades(pair))
            ),
            SETTLE_TIMEOUT,
            f"sell {pair} {trade_id}",
        )
        if not settled:
            logger.warning("Sell settle timed out for %s trade %s — continuing with estimate", pair, trade_id)

        sold_usd += this_usd
        sold_coins += this_coins
        remaining -= this_usd
        fills.append({"trade_id": trade_id, "coins": this_coins, "usd": this_usd, "rate": rate})

    if sold_usd <= 0:
        raise RuntimeError(f"sold $0 of {pair}")
    return {"sold_usd": sold_usd, "sold_coins": sold_coins, "fills": fills}


def buy_usd(pair: str, stake: float) -> dict:
    if stake < MIN_STAKE:
        raise RuntimeError(f"buy stake ${stake:.2f} below ${MIN_STAKE:.0f} minimum")
    available = get_available_capital()
    if available + 1e-6 < MIN_STAKE:
        raise RuntimeError(f"available capital ${available:.2f} too low to buy {pair}")
    use = round(min(stake, available), 2)
    if use < MIN_STAKE:
        raise RuntimeError(f"clamped stake ${use:.2f} below minimum")
    logger.info("FORCEBUY %s stake=$%.2f (available=$%.2f)", pair, use, available)
    result = _ft_request("POST", "/forcebuy", json={"pair": pair, "stake_amount": use})
    if not isinstance(result, dict):
        result = {"raw": result}
    return {"stake": use, "result": result}


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

    # Long on BASE/QUOTE ratio → BASE stronger → sell QUOTE, buy BASE
    if side == "long":
        sell_symbol, buy_symbol = ROTATE_QUOTE, ROTATE_BASE
        sell_pair, buy_pair = quote_pair(), base_pair()
    else:
        sell_symbol, buy_symbol = ROTATE_BASE, ROTATE_QUOTE
        sell_pair, buy_pair = base_pair(), quote_pair()

    trades, coins, usd = position_usd(sell_pair)
    target = usd * ROTATE_RATIO
    preview = {
        "side": side,
        "ratio_pair": ratio_pair(),
        "sell_pair": sell_pair,
        "buy_pair": buy_pair,
        "sell_usd": round(usd, 2),
        "sell_coins": coins,
        "target_usd": round(target, 2),
        "ratio": ROTATE_RATIO,
    }

    if usd <= 0 or not trades:
        msg = f"IADSS ROTATE skipped — {side.upper()} {ratio_pair()}\nNo open {sell_pair} trade to rotate from"
        logger.warning(msg)
        telegram(msg)
        return {"status": "skipped", "reason": f"no open {sell_pair} trade", **preview}

    if target < MIN_STAKE:
        msg = (
            f"IADSS ROTATE skipped — {side.upper()} {ratio_pair()}\n"
            f"{int(ROTATE_RATIO*100)}% of {sell_pair} is ${target:.2f} (min ${MIN_STAKE:.0f})"
        )
        logger.warning(msg)
        telegram(msg)
        return {"status": "skipped", "reason": "below minimum", **preview}

    telegram(
        f"IADSS ROTATE {side.upper()} — {ratio_pair()}\n"
        f"Selling ${target:.2f} of {sell_pair} ({int(ROTATE_RATIO*100)}%) → buying {buy_pair}"
    )

    sell_info = sell_usd(sell_pair, target)
    sold_usd = float(sell_info["sold_usd"])
    fee = sold_usd * (FEE_BPS / 10_000.0)
    buy_stake = round(max(0.0, sold_usd - fee), 2)

    # Give Freqtrade a beat to credit free capital after the market sell.
    wait_until(lambda: get_available_capital() >= min(buy_stake, MIN_STAKE), min(SETTLE_TIMEOUT, 20), "capital")

    buy_info = None
    buy_error = None
    try:
        buy_info = buy_usd(buy_pair, buy_stake)
        bought_usd = float(buy_info["stake"])
    except Exception as e:
        buy_error = str(e)
        bought_usd = 0.0
        logger.error("Buy failed after sell: %s", e)

    apply_book_rotation(side, sell_symbol, buy_symbol, float(sell_info["sold_coins"]), sold_usd, bought_usd)

    if buy_error:
        msg = (
            f"IADSS ROTATE PARTIAL — {side.upper()} {ratio_pair()}\n"
            f"SOLD {sell_info['sold_coins']:.6f} {sell_symbol} (~${sold_usd:.2f})\n"
            f"BUY {buy_pair} FAILED: {buy_error}\n"
            f"USD is sitting as free capital — buy manually or retry"
        )
        telegram(msg)
        return {
            "status": "partial",
            "error": buy_error,
            **preview,
            "sold_usd": round(sold_usd, 2),
            "sold_coins": sell_info["sold_coins"],
            "bought_usd": 0,
            "sell_fills": sell_info["fills"],
        }

    trade_id = (buy_info or {}).get("result", {}).get("trade_id") or (buy_info or {}).get("result", {}).get("id", "?")
    msg = (
        f"IADSS ROTATE executed — {side.upper()} {ratio_pair()}\n"
        f"Sold: {sell_info['sold_coins']:.6f} {sell_symbol}  ${sold_usd:.2f}\n"
        f"Bought: {buy_symbol}  ${bought_usd:.2f}\n"
        f"Buy trade: {trade_id}"
    )
    telegram(msg)
    logger.info(msg.replace("\n", " | "))
    return {
        "status": "rotated",
        **preview,
        "sold_usd": round(sold_usd, 2),
        "sold_coins": sell_info["sold_coins"],
        "bought_usd": round(bought_usd, 2),
        "buy_trade_id": trade_id,
        "sell_fills": sell_info["fills"],
    }


def preview_rotate(side: str) -> dict:
    side = side.lower()
    if side == "long":
        sell_pair, buy_pair = quote_pair(), base_pair()
    else:
        sell_pair, buy_pair = base_pair(), quote_pair()
    _trades, coins, usd = position_usd(sell_pair)
    target = usd * ROTATE_RATIO
    return {
        "side": side,
        "ratio_pair": ratio_pair(),
        "sell_pair": sell_pair,
        "buy_pair": buy_pair,
        "position_usd": round(usd, 2),
        "position_coins": coins,
        "target_usd": round(target, 2),
        "would_skip": usd <= 0 or target < MIN_STAKE,
        "available_capital": round(get_available_capital(), 2),
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
    telegram(f"IADSS ROTATE Early Warning — LONG {ratio_pair()}\n{ROTATE_BASE} looking stronger — waiting for sequence complete")
    return jsonify({"status": "ok", "message": "early_warning", "side": "long"}), 200


@app.route("/confirm-short", methods=["POST"])
@limiter.limit("30 per minute")
@require_token
def confirm_short():
    logger.info("SHORT early warning: %s", ratio_pair())
    telegram(f"IADSS ROTATE Early Warning — SHORT {ratio_pair()}\n{ROTATE_QUOTE} looking stronger — waiting for sequence complete")
    return jsonify({"status": "ok", "message": "early_warning", "side": "short"}), 200


@app.route("/rotate-long", methods=["POST"])
@limiter.limit("6 per minute")
@require_token
def rotate_long():
    try:
        result = execute_rotate("long")
        code = 200 if result.get("status") in ("rotated", "skipped") else 207
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
        code = 200 if result.get("status") in ("rotated", "skipped") else 207
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
        code = 200 if result.get("status") in ("rotated", "skipped") else 207
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
        base_t, base_coins, base_usd = position_usd(base_pair())
        quote_t, quote_coins, quote_usd = position_usd(quote_pair())
        total = base_usd + quote_usd
        return jsonify(
            {
                "status": "ok",
                "ratio_pair": ratio_pair(),
                "live": {
                    ROTATE_BASE: {
                        "pair": base_pair(),
                        "open_trades": len(base_t),
                        "coins": base_coins,
                        "usd": round(base_usd, 2),
                        "weight_pct": round(base_usd / total * 100, 2) if total else 0,
                    },
                    ROTATE_QUOTE: {
                        "pair": quote_pair(),
                        "open_trades": len(quote_t),
                        "coins": quote_coins,
                        "usd": round(quote_usd, 2),
                        "weight_pct": round(quote_usd / total * 100, 2) if total else 0,
                    },
                    "total_usd": round(total, 2),
                    "available_capital": round(get_available_capital(), 2),
                },
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


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "service": "iadss-rotation-tracker",
            "ratio_pair": ratio_pair(),
            "ratio": ROTATE_RATIO,
        }
    )


if __name__ == "__main__":
    logger.info("IADSS Rotation Tracker starting — %s ratio=%.2f", ratio_pair(), ROTATE_RATIO)
    app.run(host="0.0.0.0", port=5000)
