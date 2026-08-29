"""
Binance Demo + DeepSeek automated trading bot.

Workflow per cycle:
  1. Fetch latest klines from Binance Demo (/api/v3/klines)
  2. Compute quantitative indicators (SMA/EMA/RSI/MACD/Bollinger/ATR/volume)
     and send them + last N candles to DeepSeek in a strict JSON-schema prompt
  3. Parse the clean JSON decision (action, quantity, stop_loss, take_profit, confidence, reason)
  4. Enforce hard risk guardrails (reject unsafe trades)
  5. Manage positions: one protected position at a time, never stacked
  6. Place a signed MARKET order on Demo
  7. Immediately place a mandatory stop-loss + take-profit (OCO) so no position is ever left unprotected
  8. Log the decision + outcome to logs/trades.jsonl and logs/bot.log
  9. Telegram control: switch between SWING and SCALPING strategies on the fly,
     pause/resume, and receive every cycle result in a chat.

Two strategies, switchable via Telegram (the choice persists across restarts):
  - swing     — the original 1h quant setup (default)
  - scalping  — 3m momentum scalping with fast (no-thinking) LLM mode

DEMO MODE ONLY — never point this at live API keys.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
import schedule
from dotenv import load_dotenv
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

# MT5 venue adapter — all MetaTrader5 code is isolated here. Imported AFTER
# load_dotenv() so its env vars (MT5_LOGIN, MT5_PASSWORD, ...) are populated.
import mt5_venue  # noqa: E402

BINANCE_DEMO_URL = os.getenv("BINANCE_DEMO_URL", "https://demo-api.binance.com")
BINANCE_API_KEY = os.getenv("BINANCE_DEMO_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_DEMO_API_SECRET", "")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# DeepSeek V4 defaults to "thinking mode" (chain-of-thought). At hourly cadence
# the extra latency/cost is negligible and reasoning yields a more planned
# decision. Keep DEEPSEEK_MAX_TOKENS high enough that thinking doesn't starve
# the final `content` (the earlier empty-response bug). temperature is ignored
# while thinking is on.
DEEPSEEK_THINKING = os.getenv("DEEPSEEK_THINKING", "true").lower() == "true"
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "10000"))
DEEPSEEK_REASONING_EFFORT = os.getenv("DEEPSEEK_REASONING_EFFORT", "high")

SYMBOL = os.getenv("SYMBOL", "BTCUSDT")

# Legacy single-strategy envs — kept as the SWING strategy defaults so existing
# .env files keep working. Each strategy also has SWING_*/SCALP_* overrides.
INTERVAL = os.getenv("INTERVAL", "1h")
CANDLES = int(os.getenv("CANDLES", "20"))
FETCH_LIMIT = int(os.getenv("FETCH_LIMIT", "120"))
RUN_EVERY_HOURS = float(os.getenv("RUN_EVERY_HOURS", "1"))

TARGET_CAPITAL_USD = float(os.getenv("TARGET_CAPITAL_USD", "100"))
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "0.02"))
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "50"))
MIN_NOTIONAL_USD = float(os.getenv("MIN_NOTIONAL_USD", "10"))
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.55"))
# Spot venue is long-only — SELL (short) entries are only allowed when ALLOW_SHORT
# is true (e.g. a future futures mode). Default false = reject SELL entries on spot.
ALLOW_SHORT = os.getenv("ALLOW_SHORT", "false").lower() == "true"

# LLM position manager — when a position is open, DeepSeek analyzes it every cycle
# and may HOLD it, TRAIL the stop-loss (lock profit / move to breakeven), or EXIT.
# Enabled by default for ALL strategies (swing, scalping via Binance; mt5).
POSITION_MANAGER = os.getenv("POSITION_MANAGER", "true").lower() == "true"

# ── Telegram control ─────────────────────────────────────────
# Bot token from @BotFather. TELEGRAM_CHAT_ID (optional) locks control to one
# chat; if empty, the first user to send /start becomes the admin automatically.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DEFAULT_STRATEGY = os.getenv("DEFAULT_STRATEGY", "swing").lower()

# ── Strategy registry: swing (1h) + scalping (3m) ────────────
STRATEGIES = {
    "swing": {
        "label": "Swing — original 1h setup",
        "interval": os.getenv("SWING_INTERVAL", INTERVAL),
        "candles": int(os.getenv("SWING_CANDLES", str(CANDLES))),
        "fetch_limit": int(os.getenv("SWING_FETCH_LIMIT", str(FETCH_LIMIT))),
        "run_every_seconds": int(
            float(os.getenv("SWING_RUN_EVERY_MINUTES", str(RUN_EVERY_HOURS * 60))) * 60
        ),
        "fast_llm": False,  # use configured DeepSeek thinking
    },
    "scalping": {
        "label": "Scalping — 3m momentum",
        "interval": os.getenv("SCALP_INTERVAL", "3m"),
        "candles": int(os.getenv("SCALP_CANDLES", "60")),
        "fetch_limit": int(os.getenv("SCALP_FETCH_LIMIT", "300")),
        "volume_min": float(os.getenv("SCALP_VOLUME_MIN", "1.0")),
        "regime_sep_pct": float(os.getenv("SCALP_REGIME_SEP_PCT", "0.02")),
        "run_every_seconds": int(
            float(os.getenv("SCALP_RUN_EVERY_MINUTES", "5")) * 60
        ),
        "fast_llm": True,  # no thinking → sub-5s decisions
    },
    "mt5": {
        "label": "MT5 — 15m trend + 5m entry (XAUUSD + EURUSD)",
        "venue": "mt5",  # routes through mt5_venue (MetaTrader5), not Binance
        "interval": os.getenv("MT5_TREND_INTERVAL", "m15"),
        "entry_interval": os.getenv("MT5_ENTRY_INTERVAL", "m5"),
        "symbols": [
            s.strip()
            for s in os.getenv("MT5_SYMBOLS", "XAUUSD,EURUSD").split(",")
            if s.strip()
        ],
        "candles": int(os.getenv("MT5_CANDLES", "40")),
        "fetch_limit": int(os.getenv("MT5_FETCH_LIMIT", "300")),
        "volume_min": float(os.getenv("MT5_VOLUME_MIN", "1.0")),
        "regime_sep_pct": float(os.getenv("MT5_REGIME_SEP_PCT", "0.02")),
        "run_every_seconds": int(
            float(os.getenv("MT5_RUN_EVERY_MINUTES", "5")) * 60
        ),
        "fast_llm": os.getenv("MT5_FAST_LLM", "true").lower() == "true",
        "min_lot": float(os.getenv("MT5_MIN_LOT", "0.01")),
        "max_lot": float(os.getenv("MT5_MAX_LOT", "1.0")),
        "risk_pct": float(os.getenv("MT5_RISK_PCT", str(MAX_RISK_PCT))),
        "entry_rsi_min": float(os.getenv("MT5_RSI_MIN", "25")),
        "entry_rsi_max": float(os.getenv("MT5_RSI_MAX", "75")),
        "trail_min_atr": float(os.getenv("MT5_TRAIL_MIN_ATR", "0.5")),
        "min_stop_atr": float(os.getenv("MT5_MIN_STOP_ATR", "0.5")),
        "force_exit_flip": os.getenv("MT5_FORCE_EXIT_FLIP", "true").lower() == "true",
        "force_exit_loss_sl_mult": float(os.getenv("MT5_FORCE_EXIT_LOSS_SL_MULT", "0.5")),
    },
}

# Runtime state (persisted to logs/state.json so a switch survives restarts)
CURRENT_STRATEGY = DEFAULT_STRATEGY
TELEGRAM_ADMIN_CHAT = TELEGRAM_CHAT_ID
PAUSED = False
# Tracked Binance open position (entry price etc. — Binance doesn't expose it via
# the balance/orders endpoints, so we persist it here for the LLM position manager).
TRACKED_POSITION = None  # dict | None

# Derive the base asset (e.g. BTC for BTCUSDT) so we can read balances/positions
_QUOTES = ("USDT", "USDC", "BUSD", "BTC", "ETH", "BNB")
BASE_ASSET = next((SYMBOL[: -len(q)] for q in _QUOTES if SYMBOL.endswith(q)), SYMBOL)

LOG_DIR = Path("logs")
TRADES_LOG = LOG_DIR / "trades.jsonl"
HUMAN_LOG = LOG_DIR / "bot.log"
STATE_FILE = LOG_DIR / "state.json"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(message: str) -> None:
    """Append a timestamped, human-readable line to logs/bot.log and stdout."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{ts}] {message}"
    print(line, flush=True)
    try:
        with HUMAN_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        print(f"WARNING: could not write human log: {exc}", flush=True)


def append_trade(record: dict) -> None:
    """Append a structured JSON record to logs/trades.jsonl."""
    record["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with TRADES_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log(f"WARNING: could not write trades.jsonl: {exc}")


# ---------------------------------------------------------------------------
# Persisted state + Telegram control (swing / scalping switching)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    """Load persisted runtime state (strategy, admin chat, paused)."""
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state() -> None:
    """Persist runtime state so a strategy switch survives restarts."""
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "strategy": CURRENT_STRATEGY,
                    "telegram_chat_id": TELEGRAM_ADMIN_CHAT,
                    "paused": PAUSED,
                    "tracked_position": TRACKED_POSITION,
                },
                f,
                indent=2,
            )
    except OSError as exc:
        log(f"WARNING: could not write state.json: {exc}")


# ── Telegram Bot API (long-polling; requests only, no new dependency) ──
def tg_api(method: str, payload: dict):
    """Call the Telegram Bot API; returns result or None on failure."""
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise ValueError(f"Telegram API error: {data}")
    return data.get("result")


def _redact(text: str) -> str:
    """Strip the bot token from any message/URL before logging it."""
    if TELEGRAM_BOT_TOKEN:
        text = text.replace(TELEGRAM_BOT_TOKEN, "***")
    return text


def _tg_esc(text: str) -> str:
    """Escape Telegram Markdown specials so a message can never 400 on parse.

    Dynamic content (exceptions, LLM reason strings, symbol names) often contains
    '_', '*', '[' — an unbalanced one makes Telegram's strict Markdown parser
    reject the WHOLE message with 400 Bad Request (and the user gets nothing).
    Escapes them while preserving the intentional **bold** and `code` spans used
    in the notification templates.
    """
    text = text.replace("**", "\x00B\x00").replace("`", "\x00C\x00")
    text = (
        text.replace("\\", "\\\\")
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )
    return text.replace("\x00B\x00", "**").replace("\x00C\x00", "`")


def tg_send(chat_id, text: str) -> None:
    if not chat_id:
        return
    try:
        tg_api(
            "sendMessage",
            {"chat_id": chat_id, "text": _tg_esc(text), "parse_mode": "Markdown"},
        )
    except Exception as exc:  # noqa: BLE001
        log(f"WARNING: Telegram send failed: {_redact(str(exc))}")


def notify(text: str) -> None:
    """Send a message to the registered admin chat (no-op if unset)."""
    tg_send(TELEGRAM_ADMIN_CHAT, text)


def _wrap_lines(text: str, width: int = 66, indent: str = "  ") -> list:
    """Word-wrap text; continuation lines get a hanging indent."""
    words = text.split()
    lines: list = []
    cur = ""
    for w in words:
        candidate = f"{cur} {w}" if cur else w
        if len(candidate) <= width:
            cur = candidate
        else:
            lines.append(cur)
            cur = indent + w
    if cur:
        lines.append(cur)
    return lines


def reject_message(name: str, decision: dict, price: float, bot_reason: str) -> str:
    """Build a readable Telegram message for a rejected cycle (no trade)."""
    action = str(decision.get("action", "HOLD")).upper()
    conf = decision.get("confidence")
    model_reason = str(decision.get("reason", "")).strip()
    for ch in ("*", "_", "`"):  # don't break Telegram Markdown rendering
        model_reason = model_reason.replace(ch, "")
    short = bot_reason.split(" — ", 1)[0] if " — " in bot_reason else bot_reason

    lines = [f"❌ *{name}* — {action} (no entry)"]
    meta = f"💰 Price ${price:,.2f}"
    if conf is not None:
        meta += f" · confidence {conf}"
    lines.append(meta)
    lines.append("")
    if model_reason:
        lines.append(f"*{short}*")
        lines.extend(_wrap_lines(model_reason))
    else:
        lines.append(short)
    return "\n".join(lines)


def tg_get_updates(offset: int) -> list:
    try:
        return (
            tg_api(
                "getUpdates",
                {"offset": offset, "timeout": 15, "allowed_updates": ["message"]},
            )
            or []
        )
    except Exception as exc:  # noqa: BLE001
        log(f"WARNING: Telegram poll failed: {_redact(str(exc))}")
        return []


def strategy_line(name: str) -> str:
    cfg = STRATEGIES[name]
    if cfg.get("venue") == "mt5":
        return (
            f"`{name}` — {cfg['label']} | {', '.join(cfg['symbols'])} | "
            f"every {cfg['run_every_seconds'] // 60} min"
        )
    return (
        f"`{name}` — {cfg['label']} | {cfg['interval']} candles | "
        f"every {cfg['run_every_seconds'] // 60} min"
    )


HELP_TEXT = (
    "🤖 *Binance DeepSeek Bot*\n\n"
    "Commands:\n"
    "- `/status` — current strategy, price, position\n"
    "- `/strategy` — list strategies\n"
    "- `/strategy swing` — switch to swing (1h)\n"
    "- `/strategy scalping` — switch to scalping (3m)\n"
    "- `/strategy mt5` — switch to MT5 (XAUUSD+EURUSD, 15m trend + 5m entry)\n"
    "- `/pause` / `/resume` — pause / resume cycles\n"
    "- `/help` — this message\n\n"
    "The chosen strategy persists across restarts. Every cycle result is posted here."
)


def status_text() -> str:
    """Build the /status reply (live price/position best-effort)."""
    cfg = STRATEGIES[CURRENT_STRATEGY]
    if cfg.get("venue") == "mt5":
        return status_text_mt5(cfg)
    lines = [
        "📊 *Bot Status*",
        f"Strategy: `{CURRENT_STRATEGY}` — {cfg['label']}",
        f"Interval: `{cfg['interval']}` | every {cfg['run_every_seconds'] // 60} min",
        f"Paused: {'yes' if PAUSED else 'no'}",
        f"Symbol: `{SYMBOL}`",
    ]
    try:
        price = float(fetch_klines(cfg)[-1]["close"])
        base_balance = get_base_balance()
        open_orders = get_open_orders()
        lines.append(f"Last close: `${price:,.2f}`")
        lines.append(
            f"Base balance: `{base_balance:.6f} {BASE_ASSET}` | "
            f"open orders: `{len(open_orders)}`"
        )
        if TRACKED_POSITION:
            tp_ = TRACKED_POSITION
            pnl = (
                (price - tp_["entry_price"]) * tp_["quantity"]
                if tp_["side"] == "LONG"
                else (tp_["entry_price"] - price) * tp_["quantity"]
            )
            lines.append(
                f"Open: `{tp_['side']} {tp_['quantity']:.6f} {BASE_ASSET}` "
                f"@ ${tp_['entry_price']:,.2f} | PnL `${pnl:,.2f}` | "
                f"SL `${tp_.get('sl', 0):.2f}` TP `${tp_.get('tp', 0):.2f}`"
            )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"(live data unavailable: {exc})")
    return "\n".join(lines)


def status_text_mt5(cfg: dict) -> str:
    """/status reply for the MT5 strategy (account, positions, live prices)."""
    lines = [
        "📊 *Bot Status*",
        f"Strategy: `{CURRENT_STRATEGY}` — {cfg['label']}",
        f"Timeframes: `{cfg['interval']}` trend + `{cfg['entry_interval']}` entry | "
        f"every {cfg['run_every_seconds'] // 60} min",
        f"Paused: {'yes' if PAUSED else 'no'}",
        f"Symbols: `{', '.join(cfg['symbols'])}`",
    ]
    try:
        lines.append(mt5_venue.mt5_health_check())
        for symbol in cfg["symbols"]:
            positions = mt5_venue.mt5_positions(symbol)
            if positions:
                for p in positions:
                    side = "LONG" if getattr(p, "type", 0) == 0 else "SHORT"
                    lines.append(
                        f"  {symbol}: {side} {p.volume} lots @ {p.price_open} "
                        f"| SL {p.sl} TP {p.tp}"
                    )
            else:
                lines.append(f"  {symbol}: no open position")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"(MT5 live data unavailable: {exc})")
    return "\n".join(lines)


def schedule_strategy(name: str) -> None:
    """Clear and reschedule cycles for a strategy's cadence."""
    schedule.clear()
    cfg = STRATEGIES[name]
    schedule.every(cfg["run_every_seconds"]).seconds.do(run_once, name)
    log(f"Scheduled: {name} every {cfg['run_every_seconds']}s")


def set_strategy(name: str) -> None:
    """Switch strategy at runtime, persist it, reschedule + run immediately."""
    global CURRENT_STRATEGY
    if name not in STRATEGIES:
        return
    CURRENT_STRATEGY = name
    save_state()
    schedule_strategy(name)
    log(f"Strategy switched to {name} — running an immediate cycle.")
    run_once(name)


def handle_update(update: dict) -> None:
    """Process one Telegram message: admin auth + commands."""
    global TELEGRAM_ADMIN_CHAT, PAUSED
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    chat_id = str(chat_id)

    # First /start auto-registers the admin chat when none is configured.
    if not TELEGRAM_ADMIN_CHAT and text.lower().startswith("/start"):
        TELEGRAM_ADMIN_CHAT = chat_id
        save_state()
        log(f"Telegram admin chat registered: {chat_id}")

    if TELEGRAM_ADMIN_CHAT and chat_id != TELEGRAM_ADMIN_CHAT:
        return  # only the admin can control the bot

    parts = text.split()
    cmd = parts[0].lower()
    arg = parts[1].lower() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        tg_send(chat_id, HELP_TEXT)
    elif cmd == "/status":
        tg_send(chat_id, status_text())
    elif cmd == "/strategy":
        if arg in STRATEGIES:
            set_strategy(arg)
            tg_send(chat_id, f"🔄 Switched to **{arg}**\n{strategy_line(arg)}")
        else:
            avail = "\n".join(
                f"- `/strategy {n}` — {s['label']}" for n, s in STRATEGIES.items()
            )
            tg_send(chat_id, f"Usage: `/strategy <name>`\n\n{avail}")
    elif cmd == "/pause":
        PAUSED = True
        save_state()
        tg_send(chat_id, "⏸ Trading paused — cycles are skipped. Use `/resume`.")
    elif cmd == "/resume":
        PAUSED = False
        save_state()
        tg_send(chat_id, "▶ Trading resumed.")


# ---------------------------------------------------------------------------
# Phase 2 step 1 — Fetch klines
# ---------------------------------------------------------------------------
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def fetch_klines(cfg: dict) -> list:
    """Fetch the latest candles for a strategy's symbol/interval."""
    url = f"{BINANCE_DEMO_URL}/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": cfg["interval"], "limit": cfg["fetch_limit"]}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    # Binance kline row: [open_time, open, high, low, close, volume(quote? no: base),
    #                     close_time, quote_volume(USDT), trades, taker_buy_base, taker_buy_quote, ignore]
    return [
        {
            "open_time": k[0],
            "open": k[1],
            "high": k[2],
            "low": k[3],
            "close": k[4],
            "volume": k[5],        # base asset volume (e.g. BTC)
            "quote_volume": k[7],  # quote asset volume (e.g. USDT)
        }
        for k in data
    ]


# ---------------------------------------------------------------------------
# Quantitative indicators — computed in code, then fed to the model as inputs
# ---------------------------------------------------------------------------
def sma(values: list, period: int) -> float:
    """Simple moving average of the last `period` values."""
    if period <= 0 or len(values) < period:
        return float("nan")
    return sum(values[-period:]) / period


def ema_series(values: list, period: int) -> list:
    """Full EMA series (seeded with the SMA of the first `period` values)."""
    if period <= 0 or len(values) < period:
        return []
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out = [prev]
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(values: list, period: int = 14) -> float:
    """Wilder's RSI computed on the last `period` price changes."""
    if len(values) < period + 1:
        return float("nan")
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(klines: list, period: int = 14) -> float:
    """Average True Range (Wilder) — volatility used for stop placement."""
    if len(klines) < period + 1:
        return float("nan")
    trs = []
    for i in range(1, len(klines)):
        high = float(klines[i]["high"])
        low = float(klines[i]["low"])
        prev_close = float(klines[i - 1]["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    avg = sum(trs[:period]) / period
    for tr in trs[period:]:
        avg = (avg * (period - 1) + tr) / period
    return avg


def bollinger(values: list, period: int = 20, num_std: float = 2.0) -> tuple:
    """Bollinger Bands → (mid, upper, lower)."""
    if len(values) < period:
        return float("nan"), float("nan"), float("nan")
    window = values[-period:]
    mid = sum(window) / period
    variance = sum((v - mid) ** 2 for v in window) / period
    std = variance ** 0.5
    return mid, mid + num_std * std, mid - num_std * std


def macd(values: list, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """MACD → (macd_line, signal_line, histogram)."""
    ema_fast = ema_series(values, fast)
    ema_slow = ema_series(values, slow)
    if not ema_fast or not ema_slow or len(ema_slow) < signal:
        return float("nan"), float("nan"), float("nan")
    macd_line = [f - s for f, s in zip(ema_fast[-len(ema_slow):], ema_slow)]
    signal_line = ema_series(macd_line, signal)
    if not signal_line:
        return float("nan"), float("nan"), float("nan")
    return macd_line[-1], signal_line[-1], macd_line[-1] - signal_line[-1]


def macd_hist_series(values: list, fast: int = 12, slow: int = 26, signal: int = 9) -> list:
    """Full MACD histogram series (macd_line − signal_line), newest last.

    Lets the prompt verify whether the histogram is expanding (hist[i] > hist[i-1])
    instead of forcing the model to guess from a single value.
    """
    ema_fast = ema_series(values, fast)
    ema_slow = ema_series(values, slow)
    if not ema_fast or not ema_slow or len(ema_slow) < signal:
        return []
    macd_line = [f - s for f, s in zip(ema_fast[-len(ema_slow):], ema_slow)]
    signal_line = ema_series(macd_line, signal)
    if not signal_line:
        return []
    # signal_line is (signal-1) bars shorter than macd_line; align tail-to-tail
    return [
        macd_line[i] - signal_line[i - (signal - 1)]
        for i in range(signal - 1, len(macd_line))
    ]


def momentum(values: list, period: int) -> float:
    """Rate of change (%) over `period` bars."""
    if period <= 0 or len(values) < period + 1:
        return float("nan")
    prev = values[-period - 1]
    if prev == 0:
        return float("nan")
    return (values[-1] - prev) / prev * 100.0


def volume_ratio(klines: list, window: int = 20) -> float:
    """Last bar volume ÷ average volume over `window` bars (conviction check).

    Uses quote (USDT) volume when available — more stable across price levels
    and comparable across pairs — falling back to base volume otherwise.
    """
    def _vol(k):
        qv = k.get("quote_volume")
        return float(qv) if qv is not None else float(k["volume"])
    vols = [_vol(k) for k in klines]
    window_vols = vols[-window:]
    avg = sum(window_vols) / len(window_vols) if window_vols else 0.0
    return vols[-1] / avg if avg else float("nan")


def vwap(klines: list) -> float:
    """Volume-weighted average price (typical price × volume / volume) over the window."""
    tp_vol = 0.0
    vol = 0.0
    for k in klines:
        tp = (float(k["high"]) + float(k["low"]) + float(k["close"])) / 3.0
        v = float(k["volume"])
        tp_vol += tp * v
        vol += v
    return tp_vol / vol if vol else float("nan")


def fmt(value: float, digits: int = 2) -> str:
    """Format a number for the prompt; 'n/a' when not computable (NaN)."""
    if value != value:  # NaN check
        return "n/a"
    return f"{value:.{digits}f}"


# ---------------------------------------------------------------------------
# Phase 2 step 2 — DeepSeek decision (quantitative strategy prompts)
# ---------------------------------------------------------------------------
def _indicator_block(klines: list, cfg: dict, name: str = "") -> tuple:
    """Compute quant indicators + candle rows shared by all strategy prompts.
    Returns (price, indicators_text, candle_rows_text)."""
    price = float(klines[-1]["close"])
    closes = [float(k["close"]) for k in klines]

    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    bb_mid, bb_upper, bb_lower = bollinger(closes)
    macd_line, signal_line, hist = macd(closes)
    hist_series = macd_hist_series(closes)
    prev_hist = hist_series[-2] if len(hist_series) >= 2 else float("nan")
    # "expanding" is only decidable when both current and previous hist exist
    if hist_series and prev_hist == prev_hist and hist == hist:  # no NaN
        macd_expanding = "yes" if hist > prev_hist else "no"
    else:
        macd_expanding = "n/a"
    atr_val = atr(klines)
    rsi_val = rsi(closes)
    roc10 = momentum(closes, 10)
    vol_ratio = volume_ratio(klines)

    sma7 = sma(closes, 7)
    sma25 = sma(closes, 25)

    if price > sma25 and price > sma7:
        trend = "bull (price above both SMA7 and SMA25)"
    elif price < sma25 and price < sma7:
        trend = "bear (price below both SMA7 and SMA25)"
    else:
        trend = "neutral / mixed"

    pct_b = (
        (price - bb_lower) / (bb_upper - bb_lower)
        if bb_upper != bb_lower
        else float("nan")
    )

    lines = [
        f"- Last price: {price:,.2f}",
        f"- 10-bar momentum (ROC): {fmt(roc10, 2)}%",
        f"- SMA(7): {fmt(sma7)} | SMA(25): {fmt(sma25)}",
        f"- EMA(12): {fmt(ema12[-1] if ema12 else float('nan'))} "
        f"| EMA(26): {fmt(ema26[-1] if ema26 else float('nan'))}",
        f"- RSI(14): {fmt(rsi_val, 1)}",
        f"- MACD(12,26,9): line={fmt(macd_line)} signal={fmt(signal_line)} "
        f"hist={fmt(hist)} prev_hist={fmt(prev_hist)} expanding={macd_expanding}",
        f"- Bollinger(20,2σ): mid={fmt(bb_mid)} upper={fmt(bb_upper)} lower={fmt(bb_lower)} (%B={fmt(pct_b, 2)})",
        f"- ATR(14): {fmt(atr_val)} (≈{fmt(atr_val / price * 100, 2)}% of price)",
        f"- Volume ratio (USDT quote, last/avg20): {fmt(vol_ratio, 2)}",
        f"- Trend state: {trend}",
    ]

    if name in ("scalping", "mt5"):
        # Fast entry indicators — EMA12/26, MACD and SMA25 lag too much on 3m/5m.
        prefix = "SCALP" if name == "scalping" else "M5 ENTRY"
        ema9 = ema_series(closes, 9)
        ema21 = ema_series(closes, 21)
        ema9_last = ema9[-1] if ema9 else float("nan")
        ema21_last = ema21[-1] if ema21 else float("nan")
        rsi7 = rsi(closes, 7)
        roc5 = momentum(closes, 5)
        vwap_val = vwap(klines)
        if ema9_last == ema9_last:
            price_vs_ema9 = "above" if price > ema9_last else "below"
        else:
            price_vs_ema9 = "n/a"
        # Deterministic regime with a dead-zone: tiny EMA gaps (e.g. 0.002% on
        # BTC) are noise, so below the minimum separation we call it flat.
        if ema9_last == ema9_last and ema21_last == ema21_last and price:
            sep_pct = abs(ema9_last - ema21_last) / price * 100.0
        else:
            sep_pct = float("nan")
        if sep_pct == sep_pct and sep_pct >= cfg["regime_sep_pct"]:
            scalp_regime = "long" if ema9_last > ema21_last else "short"
        else:
            scalp_regime = "flat"
        lines.extend(
            [
                f"- {prefix} REGIME: {scalp_regime} (EMA9/21 sep {fmt(sep_pct, 3)}%, min {cfg['regime_sep_pct']}%)",
                f"- {prefix} FAST(9/21): EMA(9)={fmt(ema9_last)} | EMA(21)={fmt(ema21_last)}",
                f"- {prefix} RSI(7)={fmt(rsi7, 1)} | 5-bar ROC={fmt(roc5, 2)}%",
                f"- {prefix} VWAP={fmt(vwap_val)} | price {price_vs_ema9} EMA(9)",
            ]
        )

    indicators = "\n".join(lines)

    candle_rows = "\n".join(
        f"- open_time={datetime.fromtimestamp(k['open_time'] / 1000, tz=timezone.utc).isoformat()} "
        f"open={k['open']} high={k['high']} low={k['low']} close={k['close']} volume={k['volume']}"
        for k in klines[-cfg["candles"]:]
    )

    return price, indicators, candle_rows


def _long_only_note() -> str:
    """Prompt note stating shorts are not executable when ALLOW_SHORT is off."""
    if ALLOW_SHORT:
        return ""
    return (
        "\n⚠️ LONG-ONLY ACCOUNT: this venue is Binance SPOT — you CANNOT open short "
        "(SELL) positions. If the setup is short, you MUST output HOLD with quantity 0 "
        "and strategy 'none'.\n"
    )


def _swing_prompt(cfg: dict, price: float, indicators: str, candle_rows: str, n_total: int) -> str:
    """Swing (1h) — trend-following quant strategy prompt."""
    max_allowed_loss = MAX_RISK_PCT * TARGET_CAPITAL_USD
    long_only_note = _long_only_note()
    return f"""
You are a QUANTITATIVE crypto trading strategist for a DEMO account with ${TARGET_CAPITAL_USD:.0f} target capital.
Apply systematic, rules-based analysis to the computed indicators below and decide whether to open a trade on {SYMBOL} ({cfg['interval']}).

MARKET CONTEXT:
- Venue: Binance DEMO (paper trading). Spot {SYMBOL} quoted in USDT — this is CRYPTO, not equities or forex.
- Crypto trades 24/7/365: no market close and no daily-open gap, but weekend/holiday volume can be thinner and moves can be sharp and fast.
- Inputs are OHLCV only (no order book, no news, no funding data) — decide strictly from the quant indicators below; do not hallucinate news or sentiment.
- ATR(14) reflects the CURRENT crypto volatility regime; always size stops to it and never place a stop tighter than ~0.5 × ATR (that would be pure noise).

MARKET: {SYMBOL} | {cfg['interval']} | Last price: ${price:,.2f}

COMPUTED INDICATORS (calculated in code from the OHLCV history):
{indicators}

RECENT CANDLES (last {cfg['candles']} of {n_total}):
{candle_rows}
{long_only_note}
QUANT STRATEGY FRAMEWORK — apply these rules IN ORDER:
1. TREND FILTER: long-biased only if trend is bull (price > SMA25 AND EMA12 > EMA26); short-biased only if trend is bear; otherwise → HOLD.
2. MOMENTUM CONFIRMATION: for BUY require RSI(14) in 50–70 (strong, not overbought) and MACD histogram >= 0 / rising (hist > prev_hist); for SELL require RSI(14) in 30–50 and MACD histogram <= 0 / falling (hist < prev_hist). Never chase RSI > 75 or RSI < 25. Use the provided prev_hist / expanding fields to judge this — do not guess.
3. VOLUME CONFIRMATION: prefer entries when volume ratio > 1.0 (conviction). Treat ratio < 0.8 as weak — raise the bar for entry.
4. ENTRY: only enter on confluence of trend + momentum + volume. Any missing leg → HOLD with quantity 0.
5. STOP LOSS (MANDATORY for any BUY/SELL): entry ∓ 1.5 × ATR(14) — BUY stop BELOW entry, SELL stop ABOVE entry. If ATR is n/a, use a 1% band around price.
6. TAKE PROFIT: at least 2R (2 × entry→stop distance), capped by the nearest swing high (BUY) / swing low (SELL).
7. SIZING: quantity must keep notional ≤ ${MAX_POSITION_USD:.0f} USD AND projected loss (|entry − stop| × qty) ≤ ${max_allowed_loss:.2f} (2% of ${TARGET_CAPITAL_USD:.0f}).

OUTPUT — respond with ONLY a single JSON object (no markdown, no commentary), exactly this schema:
{{
  "action": "BUY" | "SELL" | "HOLD",
  "strategy": "<'trend-following' | 'mean-reversion' | 'none'>",
  "quantity": <float, base-asset quantity, 0 for HOLD>,
  "stop_loss": <float, MANDATORY for BUY/SELL, per rule 5>,
  "take_profit": <float, per rule 6>,
  "confidence": <float 0..1, how strongly the rules agree>,
  "reason": "<short quant rationale citing the actual indicator values>"
}}
"""


def _scalping_prompt(cfg: dict, price: float, indicators: str, candle_rows: str, n_total: int) -> str:
    """Scalping (3m) — fast momentum, tight TP/SL. LLM runs in fast mode."""
    max_allowed_loss = MAX_RISK_PCT * TARGET_CAPITAL_USD
    long_only_note = _long_only_note()
    return f"""
You are a QUANTITATIVE SCALPING strategist for a DEMO account with ${TARGET_CAPITAL_USD:.0f} target capital.
You trade {SYMBOL} on {cfg['interval']} candles with a short holding time (minutes). Act fast, precise and mechanical — small targets, tight stops, always protected.

MARKET CONTEXT:
- Venue: Binance DEMO (paper). Spot {SYMBOL}/USDT crypto — 24/7, high intraday noise, sharp moves.
- Inputs are OHLCV + computed indicators ONLY — no news, no sentiment. Never hallucinate fundamentals.
- {cfg['interval']} is noisy: only trade when the momentum burst is clean and volume confirms it.

MARKET: {SYMBOL} | {cfg['interval']} | Last price: ${price:,.2f}

COMPUTED INDICATORS (calculated in code from the OHLCV history):
{indicators}

RECENT CANDLES (last {cfg['candles']} of {n_total}):
{candle_rows}
{long_only_note}
SCALP STRATEGY FRAMEWORK — apply these rules IN ORDER:
FAST indicators only: use the FAST(9/21), RSI(7), 5-bar ROC, VWAP, EMA(9) and volume ratio lines below. IGNORE the slow EMA(12/26), MACD and SMA(25) lines for scalping — they lag on 3m candles and would make you miss or mistime quick bursts.
1. REGIME: use the computed SCALP REGIME line (long/short/flat) — do NOT re-derive the bias from the raw EMA(9)/EMA(21) values (tiny gaps are noise and are already handled in code). long → long-only; short → short-only; flat → HOLD.
2. MOMENTUM BURST ENTRY (fast, 3m):
   - BUY only if RSI(7) is 45–72, 5-bar ROC is >= 0 (not negative), price is AT or ABOVE EMA(9), and volume ratio >= {cfg['volume_min']}.
   - SELL mirror: RSI(7) 28–55, 5-bar ROC is <= 0 (not positive), price AT or BELOW EMA(9), volume ratio >= {cfg['volume_min']}.
3. AVOID CHASING: never enter when RSI(7) > 75 (overbought) or RSI(7) < 25 (oversold), and never in a flat/choppy range → HOLD.
4. STOP LOSS (MANDATORY for any BUY/SELL): 0.5 × ATR(14) from entry (BUY below, SELL above); NEVER wider than 1 × ATR. If ATR is n/a, use a 0.2% band around price.
5. TAKE PROFIT: a small fixed target of 0.20%–0.35% (or 1.5R if that is tighter). Risk/reward must be ≥ 1:1.
6. SIZING: quantity must keep notional ≤ ${MAX_POSITION_USD:.0f} USD AND projected loss (|entry − stop| × qty) ≤ ${max_allowed_loss:.2f} (2% of ${TARGET_CAPITAL_USD:.0f}). With tight stops this allows a meaningful quantity.
7. If any rule fails or signals conflict → HOLD with quantity 0.

OUTPUT — respond with ONLY a single JSON object (no markdown, no commentary), exactly this schema:
{{
  "action": "BUY" | "SELL" | "HOLD",
  "strategy": "<'momentum-scalp' | 'none'>",
  "quantity": <float, base-asset quantity, 0 for HOLD>,
  "stop_loss": <float, MANDATORY for BUY/SELL, per rule 4>,
  "take_profit": <float, per rule 5>,
  "confidence": <float 0..1, how strongly the rules agree>,
  "reason": "<short quant rationale citing the actual indicator values>"
}}
"""


def build_prompt(klines: list, name: str) -> str:
    """Build the strategy-specific JSON-schema prompt from computed indicators."""
    cfg = STRATEGIES[name]
    price, indicators, candle_rows = _indicator_block(klines, cfg, name)
    if name == "scalping":
        return _scalping_prompt(cfg, price, indicators, candle_rows, len(klines))
    return _swing_prompt(cfg, price, indicators, candle_rows, len(klines))


def _mt5_prompt(cfg: dict, symbol: str, price: float, trend_indicators: str,
                entry_indicators: str, trend_rows: str, entry_rows: str,
                symbol_info_line: str) -> str:
    """MT5 (forex/CFD) — 15m trend filter + 5m entry, lot-based, longs+shorts."""
    risk_pct = cfg["risk_pct"]
    return f"""
You are a QUANTITATIVE FOREX/CFD trading strategist for a METATRADER 5 DEMO account.
You trade {symbol} on {cfg['interval']} (trend) and {cfg['entry_interval']} (entry).

MARKET CONTEXT:
- Venue: MetaTrader 5 DEMO. {symbol} is a forex/CFD instrument (NOT spot crypto).
- SHORT (SELL) IS ALLOWED — this account can hold both long and short positions.
- Forex/gold trade ~24/5 (weekends closed; daily rollover/swap applies). Sessions matter.
- Inputs are OHLCV + computed indicators ONLY — no news, no sentiment. Never hallucinate fundamentals.
- Sizing is in LOTS, not base units. Contract size comes from the terminal (see SYMBOL INFO).
  quantity = NUMBER OF LOTS (e.g. 0.01, 0.10). One position per symbol is allowed.

MARKET: {symbol} | trend {cfg['interval']} + entry {cfg['entry_interval']} | Last price: ${price:,.5f}

{symbol_info_line}

TREND INDICATORS ({cfg['interval']} — higher timeframe, sets direction):
{trend_indicators}

RECENT TREND CANDLES (last {cfg['candles']}):
{trend_rows}

ENTRY INDICATORS ({cfg['entry_interval']} — lower timeframe, times the entry):
{entry_indicators}

RECENT ENTRY CANDLES (last {cfg['candles']}):
{entry_rows}

MT5 STRATEGY FRAMEWORK (HTF trend + LTF entry confluence):
1. TREND FILTER ({cfg['interval']}): trade WITH the higher-timeframe trend. Use SMA(25)/EMA(12,26), MACD and RSI from the TREND block. bull → only BUY; bear → only SELL; neutral/mixed → HOLD.
2. ENTRY TRIGGER ({cfg['entry_interval']}): on the ENTRY block use the M5 ENTRY REGIME + FAST(9/21) + RSI(7) + VWAP + volume ratio. Enter only when {cfg['entry_interval']} momentum agrees with the trend (BUY: regime long, RSI(7) 50-72, price >= EMA(9), volume ratio >= {cfg['volume_min']}; SELL mirrored). Never chase RSI(7) > 75 or < 25.
3. VOLUME: prefer volume ratio > 1.0 on the entry timeframe; treat < 0.8 as weak (raise the bar).
4. STOP LOSS (MANDATORY for any BUY/SELL): sized to ATR(14) from the ENTRY timeframe — BUY stop BELOW entry, SELL stop ABOVE entry; roughly 1.0-1.5 x ATR, never tighter than ~0.5 x ATR. Round to the symbol's tick size.
5. TAKE PROFIT: at least 1.5-2R (2 x entry-to-stop distance), capped by the nearest swing level.
6. SIZING (LOTS): pick the LOT SIZE so projected loss = |entry - stop| x lots x contract_size <= {risk_pct * 100:.1f}% of account balance. Stay within the terminal's min/max lot (see SYMBOL INFO). Default to a small lot (e.g. 0.01) when unsure.
7. If any rule fails or signals conflict → HOLD with quantity 0.

OUTPUT — respond with ONLY a single JSON object (no markdown, no commentary), exactly this schema:
{{
  "action": "BUY" | "SELL" | "HOLD",
  "strategy": "<'trend-following' | 'none'>",
  "quantity": <float, NUMBER OF LOTS, 0 for HOLD>,
  "stop_loss": <float, price of the stop, MANDATORY for BUY/SELL>,
  "take_profit": <float, price of the take-profit>,
  "confidence": <float 0..1, how strongly the rules agree>,
  "reason": "<short quant rationale citing the actual indicator values>"
}}
"""


def build_mt5_prompt(symbol: str, cfg: dict, trend_klines: list, entry_klines: list) -> str:
    """Build the MT5 prompt from the two timeframe candle sets."""
    # 'mt5_trend' → base indicators only (no fast entry block); 'mt5' → base + fast.
    _, trend_indicators, trend_rows = _indicator_block(trend_klines, cfg, "mt5_trend")
    price, entry_indicators, entry_rows = _indicator_block(entry_klines, cfg, "mt5")
    info = mt5_venue.mt5_symbol_info(symbol)
    symbol_info_line = (
        f"SYMBOL INFO ({symbol}): contract_size={info.trade_contract_size} | "
        f"lots {info.volume_min}-{info.volume_max} step {info.volume_step} | "
        f"digits={info.digits} point={info.point}"
    )
    return _mt5_prompt(cfg, symbol, price, trend_indicators, entry_indicators,
                       trend_rows, entry_rows, symbol_info_line)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    reraise=True,
)
def ask_deepseek(prompt: str, fast: bool = False) -> str:
    """Send the prompt to DeepSeek via its OpenAI-compatible endpoint.

    `fast=True` disables thinking and caps tokens (~sub-5s) — used by scalping.
    """
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    thinking_on = DEEPSEEK_THINKING and not fast
    kwargs: dict = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a JSON-only trading analyst. Always output valid JSON.",
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2000 if fast else DEEPSEEK_MAX_TOKENS,
        "extra_body": {
            "thinking": {"type": "enabled" if thinking_on else "disabled"}
        },
    }
    # temperature is ignored while thinking mode is enabled
    if thinking_on:
        kwargs["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT
    else:
        kwargs["temperature"] = 0.1 if fast else 0.2

    resp = client.chat.completions.create(**kwargs)

    if not resp.choices:
        raise ValueError("DeepSeek returned no choices")

    choice = resp.choices[0]
    message = choice.message
    finish_reason = getattr(choice, "finish_reason", None)
    content = (message.content or "").strip()

    if not content:
        raise ValueError(
            f"DeepSeek returned an empty response "
            f"(model={DEEPSEEK_MODEL}, thinking={DEEPSEEK_THINKING}, "
            f"finish_reason={finish_reason}). Verify DEEPSEEK_API_KEY and "
            f"DEEPSEEK_MODEL in .env."
        )

    log(f"DeepSeek finish_reason={finish_reason}, content_len={len(content)}")
    return content


# ---------------------------------------------------------------------------
# Phase 2 step 3 — Parse clean JSON
# ---------------------------------------------------------------------------
def parse_decision(raw: str) -> dict:
    """Extract a clean JSON object from the model's raw response."""
    text = raw.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback: locate the first { ... } block
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"Could not parse JSON from model output: {raw!r}")
        return json.loads(text[start : end + 1])


# ---------------------------------------------------------------------------
# Phase 2 step 4 — Hard risk guardrails
# ---------------------------------------------------------------------------
def validate_decision(decision: dict, price: float) -> tuple:
    """Enforce hard risk guardrails. Returns (ok: bool, reason: str)."""
    action = str(decision.get("action", "HOLD")).upper()
    if action not in {"BUY", "SELL", "HOLD"}:
        return False, f"Rejected: unknown action {decision.get('action')!r}"
    if action == "HOLD":
        model_reason = str(decision.get("reason", "")).strip()
        suffix = f" — {model_reason}" if model_reason else ""
        return False, f"Rejected: model returned HOLD{suffix}"
    if action == "SELL" and not ALLOW_SHORT:
        return False, "Rejected: spot venue is long-only — SELL (short) entries not supported"

    quantity = float(decision.get("quantity", 0) or 0)
    stop_loss = decision.get("stop_loss")
    confidence = float(decision.get("confidence", 0) or 0)

    # Guardrail 1 — stop_loss is mandatory
    if not stop_loss:
        return False, "Rejected: no stop_loss provided"
    stop_loss = float(stop_loss)

    # Guardrail 1b — take_profit must be on the correct side (if provided)
    take_profit = decision.get("take_profit")
    if take_profit:
        take_profit = float(take_profit)
        if action == "BUY" and take_profit <= price:
            return False, "Rejected: take_profit not above the current price"
        if action == "SELL" and take_profit >= price:
            return False, "Rejected: take_profit not below the current price"

    # Guardrail 2 — confidence threshold
    if confidence < CONFIDENCE_THRESHOLD:
        return (
            False,
            f"Rejected: confidence {confidence:.2f} < threshold {CONFIDENCE_THRESHOLD}",
        )

    notional = quantity * price

    # Guardrail 3 — minimum notional
    if notional < MIN_NOTIONAL_USD:
        return False, f"Rejected: notional ${notional:.2f} < min ${MIN_NOTIONAL_USD:.2f}"

    # Guardrail 4 — maximum position
    if notional > MAX_POSITION_USD:
        return False, f"Rejected: notional ${notional:.2f} > max ${MAX_POSITION_USD:.2f}"

    # Guardrail 5 — projected loss must not exceed 2% of target capital
    if action == "BUY":
        loss_per_unit = price - stop_loss
    else:  # SELL
        loss_per_unit = stop_loss - price
    if loss_per_unit <= 0:
        return False, "Rejected: stop_loss on the wrong side of the current price"
    max_allowed_loss = MAX_RISK_PCT * TARGET_CAPITAL_USD
    projected_loss = loss_per_unit * quantity
    if projected_loss > max_allowed_loss:
        return (
            False,
            f"Rejected: projected loss ${projected_loss:.2f} > max allowed ${max_allowed_loss:.2f}",
        )

    return True, "OK"


def validate_mt5_decision(decision: dict, price: float, symbol: str, cfg: dict,
                          entry_rsi7: float = None, entry_vol: float = None,
                          entry_atr: float = None) -> tuple:
    """MT5 risk guardrails: shorts allowed, lot bounds, SL/TP side, risk cap,
    plus hard no-chase (RSI), volume and min-stop-distance gates for entries.
    Returns (ok: bool, reason: str).
    """
    action = str(decision.get("action", "HOLD")).upper()
    if action not in {"BUY", "SELL", "HOLD"}:
        return False, f"Rejected: unknown action {decision.get('action')!r}"
    if action == "HOLD":
        model_reason = str(decision.get("reason", "")).strip()
        suffix = f" — {model_reason}" if model_reason else ""
        return False, f"Rejected: model returned HOLD{suffix}"
    # SELL (shorts) ARE allowed on MT5 (forex/CFD) — no long-only veto here.

    lots = float(decision.get("quantity", 0) or 0)
    stop_loss = decision.get("stop_loss")
    take_profit = decision.get("take_profit")
    confidence = float(decision.get("confidence", 0) or 0)

    if not stop_loss:
        return False, "Rejected: no stop_loss provided"
    stop_loss = float(stop_loss)
    if take_profit:
        take_profit = float(take_profit)
        if action == "BUY" and take_profit <= price:
            return False, "Rejected: take_profit not above the current price"
        if action == "SELL" and take_profit >= price:
            return False, "Rejected: take_profit not below the current price"

    if confidence < CONFIDENCE_THRESHOLD:
        return (
            False,
            f"Rejected: confidence {confidence:.2f} < threshold {CONFIDENCE_THRESHOLD}",
        )

    if lots <= 0:
        return False, f"Rejected: quantity (lots) must be > 0, got {lots}"

    info = mt5_venue.mt5_symbol_info(symbol)
    min_lot = max(cfg["min_lot"], float(getattr(info, "volume_min", 0) or cfg["min_lot"]))
    max_lot = min(cfg["max_lot"], float(getattr(info, "volume_max", 0) or cfg["max_lot"]))
    if lots < min_lot or lots > max_lot:
        return False, f"Rejected: lots {lots} outside allowed range [{min_lot}, {max_lot}]"

    if action == "BUY":
        loss_per_unit = price - stop_loss
    else:  # SELL
        loss_per_unit = stop_loss - price
    if loss_per_unit <= 0:
        return False, "Rejected: stop_loss on the wrong side of the current price"

    balance = mt5_venue.mt5_account_balance()
    contract_size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
    projected_loss = loss_per_unit * lots * contract_size
    max_allowed = cfg["risk_pct"] * balance
    if projected_loss > max_allowed:
        return (
            False,
            f"Rejected: projected loss ${projected_loss:.2f} > max allowed "
            f"${max_allowed:.2f} ({cfg['risk_pct'] * 100:.0f}% of {balance:.2f})",
        )

    # Min stop distance: never open with a stop tighter than ~0.5 x ATR(entry)
    # (was prompt-only — tight 1xATR stops were getting clipped by 5m noise).
    if entry_atr is not None and entry_atr == entry_atr:  # not NaN
        min_stop = cfg.get("min_stop_atr", 0.5) * entry_atr
        if loss_per_unit < min_stop:
            return (
                False,
                f"Rejected: stop {loss_per_unit:.5f} from price < min "
                f"{min_stop:.5f} ({cfg.get('min_stop_atr', 0.5):.2f} x ATR {entry_atr:.5f})",
            )

    # Hard no-chase / volume gates (were prompt-only — now enforced in code so a
    # low-volume or oversold/overbought entry can't slip through the LLM).
    if entry_rsi7 is not None and entry_rsi7 == entry_rsi7:  # not NaN
        rsi_min = cfg.get("entry_rsi_min", 25)
        rsi_max = cfg.get("entry_rsi_max", 75)
        if entry_rsi7 < rsi_min or entry_rsi7 > rsi_max:
            return (
                False,
                f"Rejected: entry RSI(7) {entry_rsi7:.1f} outside no-chase band "
                f"[{rsi_min:.0f}, {rsi_max:.0f}]",
            )
    if entry_vol is not None and entry_vol == entry_vol:  # not NaN
        vol_min = cfg.get("volume_min", 1.0)
        if entry_vol < vol_min:
            return (
                False,
                f"Rejected: entry volume ratio {entry_vol:.2f} < {vol_min:.2f} (weak tape)",
            )

    return True, "OK"


# ---------------------------------------------------------------------------
# Phase 2 step 5 — Signed MARKET order on Demo
# ---------------------------------------------------------------------------
def signed_params(params: dict) -> dict:
    """Sign Binance API params with HMAC-SHA256."""
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    query = urllib.parse.urlencode(params)
    signature = hmac.new(
        BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    params["signature"] = signature
    return params


def signed_headers() -> dict:
    """Headers required by every signed Binance endpoint (X-MBX-APIKEY)."""
    return {"X-MBX-APIKEY": BINANCE_API_KEY}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def place_market_order(decision: dict, price: float) -> dict:
    """Place a signed MARKET order on the Binance Demo exchange."""
    action = decision["action"].upper()
    side = "BUY" if action == "BUY" else "SELL"
    quantity = f"{float(decision['quantity']):.8f}"
    params = signed_params(
        {
            "symbol": SYMBOL,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
        }
    )
    url = f"{BINANCE_DEMO_URL}/api/v3/order"
    resp = requests.post(url, params=params, headers=signed_headers(), timeout=15)
    resp.raise_for_status()
    order = resp.json()
    log(
        f"ORDER FILLED: {side} {quantity} {SYMBOL} @ ~${price:.2f} "
        f"| orderId={order.get('orderId')} status={order.get('status')}"
    )
    return order


# ---------------------------------------------------------------------------
# Position protection — the "Samuel Phase E" fix: real SL/TP on the exchange
# ---------------------------------------------------------------------------
def opposite_side(action: str) -> str:
    """Return the closing side for an entry action."""
    return "SELL" if action.upper() == "BUY" else "BUY"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def get_open_orders() -> list:
    """Fetch all open orders for the symbol (signed)."""
    url = f"{BINANCE_DEMO_URL}/api/v3/openOrders"
    resp = requests.get(
        url, params=signed_params({"symbol": SYMBOL}), headers=signed_headers(), timeout=15
    )
    resp.raise_for_status()
    return resp.json()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def get_base_balance() -> float:
    """Return the free balance of the base asset (e.g. BTC for BTCUSDT)."""
    url = f"{BINANCE_DEMO_URL}/api/v3/account"
    resp = requests.get(
        url, params=signed_params({}), headers=signed_headers(), timeout=15
    )
    resp.raise_for_status()
    for bal in resp.json().get("balances", []):
        if bal["asset"] == BASE_ASSET:
            return float(bal.get("free", 0))
    return 0.0


def cancel_open_orders() -> None:
    """Cancel all open orders for the symbol (signed)."""
    url = f"{BINANCE_DEMO_URL}/api/v3/openOrders"
    resp = requests.delete(
        url, params=signed_params({"symbol": SYMBOL}), headers=signed_headers(), timeout=15
    )
    resp.raise_for_status()
    cancelled = resp.json()
    log(f"Cancelled {len(cancelled)} open order(s)")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def place_stop_loss(side: str, quantity: float, stop_loss: float) -> dict:
    """Place a standalone hard STOP_LOSS_LIMIT (used when no take-profit is given)."""
    params = signed_params(
        {
            "symbol": SYMBOL,
            "side": side,
            "type": "STOP_LOSS_LIMIT",
            "quantity": f"{quantity:.8f}",
            "price": f"{stop_loss:.2f}",
            "stopPrice": f"{stop_loss:.2f}",
            "timeInForce": "GTC",
        }
    )
    url = f"{BINANCE_DEMO_URL}/api/v3/order"
    resp = requests.post(url, params=params, headers=signed_headers(), timeout=15)
    resp.raise_for_status()
    order = resp.json()
    log(
        f"STOP-LOSS placed: {side} {quantity:.8f} {SYMBOL} @ {stop_loss} "
        f"(orderId={order.get('orderId')})"
    )
    return order


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def place_oco(side: str, quantity: float, stop_loss: float, take_profit: float) -> dict:
    """Place a single OCO (stop-loss + take-profit). OCO locks the quantity ONCE
    and auto-cancels the losing leg when the winning leg fills — the exact
    protection Samuel lacked in Phase E."""
    params = signed_params(
        {
            "symbol": SYMBOL,
            "side": side,
            "quantity": f"{quantity:.8f}",
            "aboveType": "LIMIT_MAKER",
            "abovePrice": f"{take_profit:.2f}",
            "belowType": "STOP_LOSS_LIMIT",
            "belowPrice": f"{stop_loss:.2f}",
            "belowStopPrice": f"{stop_loss:.2f}",
        }
    )
    url = f"{BINANCE_DEMO_URL}/api/v3/orderList/oco"
    resp = requests.post(url, params=params, headers=signed_headers(), timeout=15)
    resp.raise_for_status()
    order = resp.json()
    log(
        f"OCO PROTECTION placed: {side} {quantity:.8f} {SYMBOL} "
        f"(SL {stop_loss} / TP {take_profit}) orderListId={order.get('orderListId')}"
    )
    return order


def place_protective_orders(decision: dict, quantity: float) -> str:
    """After an entry fill, protect the position. Returns a label of what was placed."""
    side = opposite_side(decision["action"])
    stop_loss = float(decision["stop_loss"])
    take_profit = decision.get("take_profit")
    if take_profit:
        place_oco(side, quantity, stop_loss, float(take_profit))
        return "OCO(SL+TP)"
    place_stop_loss(side, quantity, stop_loss)
    return "SL"


def safety_close_position(side: str, quantity: float) -> None:
    """Best-effort market close if protective orders fail — never go naked."""
    try:
        params = signed_params(
            {
                "symbol": SYMBOL,
                "side": side,
                "type": "MARKET",
                "quantity": f"{quantity:.8f}",
            }
        )
        url = f"{BINANCE_DEMO_URL}/api/v3/order"
        resp = requests.post(url, params=params, headers=signed_headers(), timeout=15)
        resp.raise_for_status()
        log(
            f"SAFETY CLOSE: closed {quantity:.8f} {SYMBOL} "
            f"(orderId={resp.json().get('orderId')})"
        )
    except Exception as exc:  # noqa: BLE001
        log(f"CRITICAL: safety close failed ({exc}) — position may be unprotected!")


def verify_credentials() -> bool:
    """Return True if the demo API credentials are accepted; otherwise log a
    clear message and return False (avoids an hourly 401 error loop)."""
    try:
        get_base_balance()
        return True
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 401:
            log(
                "ERROR: Binance Demo API rejected the signed request "
                "(401 / -2014). Check BINANCE_DEMO_API_KEY / "
                "BINANCE_DEMO_API_SECRET in .env and that the demo account "
                "has API access enabled, then restart."
            )
            return False
        raise


# ---------------------------------------------------------------------------
# LLM position manager — DeepSeek analyzes ANY open position every cycle and may
# HOLD it, TRAIL the stop-loss (lock profit / breakeven), or EXIT it now.
# Applies to all three strategies (swing + scalping on Binance, mt5 on MT5).
# ---------------------------------------------------------------------------

def _tracked_position() -> dict:
    """Persisted Binance position (entry price survives restarts)."""
    return TRACKED_POSITION or {}


def track_binance_position(name: str, decision: dict, filled_qty: float,
                           fill_price: float, protection: str) -> None:
    """Record the just-opened Binance position so the LLM manager knows its entry."""
    global TRACKED_POSITION
    TRACKED_POSITION = {
        "symbol": SYMBOL,
        "side": "LONG" if decision["action"].upper() == "BUY" else "SHORT",
        "quantity": filled_qty,
        "entry_price": fill_price,
        "sl": float(decision.get("stop_loss") or 0),
        "tp": float(decision.get("take_profit") or 0),
        "protection": protection,
        "strategy": name,
        "opened_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_state()


def clear_tracked_binance_position() -> None:
    """Position closed — drop the tracked record."""
    global TRACKED_POSITION
    TRACKED_POSITION = None
    save_state()


def binance_position_dict(open_orders: list, base_balance: float, price: float) -> dict:
    """Normalize the Binance open position into the shared manager shape."""
    side = "LONG"
    sl = tp = None
    for o in open_orders:
        typ = str(o.get("type", "")).upper()
        os_side = str(o.get("side", "")).upper()
        if typ == "STOP_LOSS_LIMIT":
            sl = float(o.get("stopPrice") or o.get("price") or 0)
            side = "SHORT" if os_side == "BUY" else "LONG"
        elif typ == "LIMIT_MAKER":
            tp = float(o.get("price") or 0)
    tracked = _tracked_position()
    entry = float(tracked.get("entry_price") or price)
    qty = base_balance
    pnl = (price - entry) * qty if side == "LONG" else (entry - price) * qty
    invested = entry * qty
    return {
        "venue": "binance",
        "symbol": SYMBOL,
        "side": side,
        "quantity": qty,
        "entry_price": entry,
        "sl": sl or 0.0,
        "tp": tp or 0.0,
        "current_price": price,
        "pnl": pnl,
        "pnl_pct": (pnl / invested * 100.0) if invested else 0.0,
        "open_orders": len(open_orders),
    }


def mt5_position_dict(p, current_price: float, contract_size: float) -> dict:
    """Normalize one MT5 position (from the live terminal) into manager shape."""
    ptype = int(getattr(p, "type", 0) or 0)
    entry = float(getattr(p, "price_open", 0.0) or 0.0)
    qty = float(getattr(p, "volume", 0.0) or 0.0)
    profit = float(getattr(p, "profit", 0.0) or 0.0)
    invested = entry * qty * contract_size
    return {
        "venue": "mt5",
        "symbol": str(getattr(p, "symbol", "")),
        "ticket": int(getattr(p, "ticket", 0) or 0),
        "side": "LONG" if ptype == 0 else "SHORT",
        "quantity": qty,
        "entry_price": entry,
        "sl": float(getattr(p, "sl", 0.0) or 0.0),
        "tp": float(getattr(p, "tp", 0.0) or 0.0),
        "current_price": current_price,
        "pnl": profit,
        "pnl_pct": (profit / invested * 100.0) if invested else 0.0,
    }


def _manage_prompt(cfg: dict, price: float, pos: dict, klines: list, name: str) -> str:
    """Binance position-management prompt (shared by swing + scalping)."""
    _, indicators, _ = _indicator_block(klines, cfg, name)
    entry_s = f"${pos['entry_price']:,.2f}"
    sl_s = f"${pos['sl']:,.2f}" if pos["sl"] else "none"
    tp_s = f"${pos['tp']:,.2f}" if pos["tp"] else "none"
    return f"""
You are a QUANTITATIVE POSITION MANAGER for a DEMO Binance SPOT account (${TARGET_CAPITAL_USD:.0f} target).
A position is OPEN on {SYMBOL}. Decide whether to HOLD it, TRAIL its stop-loss (lock profit / move to breakeven), or EXIT (market-close) it now.

OPEN POSITION ({name} strategy, {cfg['interval']}):
- Side: {pos['side']} | Quantity: {pos['quantity']:.8f} {BASE_ASSET}
- Entry: {entry_s} | Current: ${price:,.2f} | Unrealized PnL: ${pos['pnl']:,.2f} ({pos['pnl_pct']:.2f}%)
- Stop-loss: {sl_s} | Take-profit: {tp_s}

CURRENT MARKET INDICATORS ({cfg['interval']}):
{indicators}

MANAGEMENT RULES — apply IN ORDER:
1. EXIT if the trade thesis has broken: trend flipped against you (LONG now in bear/neutral-mixed, SHORT now in bull), momentum strongly against (e.g. RSI(14) < 45 for a LONG, > 55 for a SHORT, or MACD histogram flipped), or price reached/overshot the take-profit.
2. TRAIL to breakeven (new stop = entry) once unrealized profit >= ~1.0 x ATR(14); then trail the stop ~1.5 x ATR behind price (LONG stop below, SHORT stop above) to lock in gains. Never tighten inside ~0.5 x ATR noise.
3. HOLD otherwise — trend with you, profit building, not yet at breakeven.

HARD CONSTRAINTS:
- LONG: new_stop_loss must be >= current stop-loss AND strictly < current price (never widen, never past price).
- SHORT: new_stop_loss must be <= current stop-loss AND strictly > current price.
- Never modify take-profit. EXIT only with a reason citing real indicator values.

OUTPUT — respond with ONLY a single JSON object (no markdown, no commentary), exactly this schema:
{{
  "action": "HOLD" | "TRAIL" | "EXIT",
  "new_stop_loss": <float, price of the new stop, REQUIRED for TRAIL only>,
  "confidence": <float 0..1>,
  "reason": "<short quant rationale citing the actual indicator values>"
}}
"""


def _mt5_manage_prompt(cfg: dict, symbol: str, pos: dict,
                       trend_klines: list, entry_klines: list) -> str:
    """MT5 position-management prompt (uses both live timeframes)."""
    _, trend_indicators, _ = _indicator_block(trend_klines, cfg, "mt5_trend")
    price, entry_indicators, _ = _indicator_block(entry_klines, cfg, "mt5")
    sl_s = f"{pos['sl']:.5f}" if pos["sl"] else "none"
    tp_s = f"{pos['tp']:.5f}" if pos["tp"] else "none"
    return f"""
You are a QUANTITATIVE POSITION MANAGER for a METATRADER 5 DEMO account.
A position is OPEN on {symbol}. Decide whether to HOLD it, TRAIL its stop-loss (lock profit / breakeven), or EXIT (close) it now.

OPEN POSITION:
- Side: {pos['side']} | Lots: {pos['quantity']:.2f}
- Entry: {pos['entry_price']:.5f} | Current: {price:.5f} | Unrealized PnL: ${pos['pnl']:,.2f} ({pos['pnl_pct']:.2f}%)
- Stop-loss: {sl_s} | Take-profit: {tp_s}

TREND INDICATORS ({cfg['interval']} — direction):
{trend_indicators}

ENTRY INDICATORS ({cfg['entry_interval']} — momentum):
{entry_indicators}

MANAGEMENT RULES — apply IN ORDER:
1. EXIT if the higher-timeframe trend has flipped against the position (LONG now in bear/neutral-mixed, SHORT now in bull), or entry-timeframe momentum is strongly against (RSI(7) extremes), or price reached/overshot the take-profit.
2. TRAIL to breakeven once unrealized profit >= ~1.0 x ATR(14) of the entry timeframe; then trail ~1.5 x ATR behind price (LONG stop below, SHORT stop above). Never tighten inside ~0.5 x ATR noise.
3. HOLD otherwise.

HARD CONSTRAINTS:
- LONG: new_stop_loss must be >= current stop-loss AND strictly < current price.
- SHORT: new_stop_loss must be <= current stop-loss AND strictly > current price.
- Never modify take-profit. EXIT only with a reason citing real indicator values.

OUTPUT — respond with ONLY a single JSON object (no markdown, no commentary), exactly this schema:
{{
  "action": "HOLD" | "TRAIL" | "EXIT",
  "new_stop_loss": <float, price of the new stop, REQUIRED for TRAIL only>,
  "confidence": <float 0..1>,
  "reason": "<short quant rationale citing the actual indicator values>"
}}
"""


def validate_manage_decision(decision: dict, pos: dict,
                             min_trail_dist: float = 0.0) -> tuple:
    """Hard guardrails for a position-management decision. Returns (ok, reason)."""
    action = str(decision.get("action", "HOLD")).upper()
    if action not in {"HOLD", "TRAIL", "EXIT"}:
        return False, f"Rejected: unknown manage action {decision.get('action')!r}"
    if action == "HOLD":
        model_reason = str(decision.get("reason", "")).strip()
        suffix = f" — {model_reason}" if model_reason else ""
        return False, f"Manager returned HOLD{suffix}"
    if action == "EXIT":
        return True, "OK"

    new_sl = decision.get("new_stop_loss")
    if not new_sl:
        return False, "Rejected: TRAIL requires new_stop_loss"
    try:
        new_sl = float(new_sl)
    except (TypeError, ValueError):
        return False, f"Rejected: new_stop_loss {new_sl!r} is not a number"

    cur = float(pos["current_price"])
    old = float(pos.get("sl") or 0.0)
    side = str(pos["side"]).upper()
    if side == "LONG":
        lower = old if old > 0 else 0.0
        if not (lower <= new_sl < cur):
            return (
                False,
                f"Rejected: new SL {new_sl:.5f} outside [{lower:.5f}, {cur:.5f}) for LONG "
                "(never widen, never past price)",
            )
    else:
        upper = old if old > 0 else float("inf")
        if not (cur < new_sl <= upper):
            return (
                False,
                f"Rejected: new SL {new_sl:.5f} outside ({cur:.5f}, {upper:.5f}] for SHORT "
                "(never widen, never past price)",
            )
    # Never tighten inside the ~0.5 x ATR noise floor (was prompt-only).
    if min_trail_dist > 0 and abs(new_sl - cur) < min_trail_dist:
        return (
            False,
            f"Rejected: new SL {new_sl:.5f} only {abs(new_sl - cur):.5f} from price "
            f"< min trail {min_trail_dist:.5f} (noise floor)",
        )
    return True, "OK"


def manage_binance_position(name: str, cfg: dict, klines: list,
                            open_orders: list, base_balance: float) -> None:
    """Analyze the open Binance position with the LLM; HOLD / TRAIL / EXIT."""
    price = float(klines[-1]["close"])
    pos = binance_position_dict(open_orders, base_balance, price)

    prompt = _manage_prompt(cfg, price, pos, klines, name)
    raw = ask_deepseek(prompt, fast=cfg["fast_llm"])
    log(f"DeepSeek manage raw response: {raw[:500]}")

    decision = parse_decision(raw)
    log(
        f"Manage decision: action={decision.get('action')} "
        f"new_sl={decision.get('new_stop_loss')} conf={decision.get('confidence')}"
    )

    ok, reason = validate_manage_decision(decision, pos)
    if not ok:
        log(f"MANAGE SKIPPED: {reason}")
        append_trade(
            {"status": "manage_hold", "venue": "binance", "reason": reason,
             "decision": decision, "position": pos}
        )
        notify(f"🤖 `{name}` position: {reason[:220]}")
        return

    action = decision["action"]
    close_side = "SELL" if pos["side"] == "LONG" else "BUY"
    if action == "EXIT":
        cancel_open_orders()
        safety_close_position(close_side, base_balance)
        clear_tracked_binance_position()
        append_trade(
            {"status": "manage_exit", "venue": "binance", "decision": decision,
             "position": pos}
        )
        notify(
            f"🚪 `{name}`: LLM EXIT {pos['side']} {pos['quantity']:.6f} {SYMBOL} "
            f"@ ${price:,.2f} — {str(decision.get('reason', ''))[:160]}"
        )
        return

    # TRAIL — cancel old protection, re-place with the new (tighter) stop.
    new_sl = float(decision["new_stop_loss"])
    old_sl = pos["sl"]
    tp = pos["tp"]
    cancel_open_orders()
    try:
        if tp:
            place_oco(close_side, base_balance, new_sl, tp)
        else:
            place_stop_loss(close_side, base_balance, new_sl)
    except Exception as exc:  # noqa: BLE001 — never leave it unprotected
        log(f"CRITICAL: trail protection failed ({exc}); safety-closing.")
        safety_close_position(close_side, base_balance)
        clear_tracked_binance_position()
        return
    if TRACKED_POSITION:
        TRACKED_POSITION["sl"] = new_sl
        save_state()
    append_trade(
        {"status": "manage_trail", "venue": "binance", "new_stop_loss": new_sl,
         "old_stop_loss": old_sl, "decision": decision, "position": pos}
    )
    notify(
        f"🪝 `{name}`: LLM TRAIL SL {old_sl or 'none'} → {new_sl:.2f} "
        f"({pos['side']} {pos['quantity']:.6f} {SYMBOL}) — {str(decision.get('reason', ''))[:160]}"
    )


def _mt5_forced_exit_reason(cfg: dict, pos: dict, price: float,
                            trend_klines: list, entry_klines: list) -> str:
    """Deterministic EXIT safety net. Returns a reason string if the position must
    be closed, else "".

    Only applies to bot-protected positions (those carrying a stop-loss — manual
    naked trades are left alone). Two triggers:
      1) higher-timeframe trend clearly flipped against the position;
      2) adverse move >= a fraction of the entry-to-SL distance (cut losses
         before they ride all the way to the broker stop).
    """
    side = str(pos["side"]).upper()
    sl = float(pos.get("sl") or 0.0)
    if sl <= 0:
        return ""  # not a bot-placed/protected position — leave it alone
    entry = float(pos["entry_price"])

    if cfg.get("force_exit_flip", True):
        trend_closes = [float(k["close"]) for k in trend_klines]
        if trend_closes:
            t_price = trend_closes[-1]
            t_sma7 = sma(trend_closes, 7)
            t_sma25 = sma(trend_closes, 25)
            if t_sma7 == t_sma7 and t_sma25 == t_sma25:  # not NaN
                if t_price > t_sma7 and t_price > t_sma25:
                    t_trend = "bull"
                elif t_price < t_sma7 and t_price < t_sma25:
                    t_trend = "bear"
                else:
                    t_trend = "neutral"
                if (side == "LONG" and t_trend == "bear") or \
                   (side == "SHORT" and t_trend == "bull"):
                    return (
                        f"safety: {cfg['interval']} trend flipped to {t_trend} "
                        f"against {side} position"
                    )

    loss_mult = cfg.get("force_exit_loss_sl_mult", 0.5)
    if loss_mult > 0:
        sl_dist = abs(entry - sl)
        adverse = (entry - price) if side == "LONG" else (price - entry)
        if adverse >= loss_mult * sl_dist:
            return (
                f"safety: adverse move {adverse:.5f} >= {loss_mult:.0%} of SL "
                f"distance ({sl_dist:.5f})"
            )
    return ""


def manage_mt5_position(name: str, cfg: dict, symbol: str, p,
                        trend_klines: list, entry_klines: list, price: float) -> None:
    """Analyze the open MT5 position with the LLM; HOLD / TRAIL / EXIT."""
    info = mt5_venue.mt5_symbol_info(symbol)
    contract_size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
    pos = mt5_position_dict(p, price, contract_size)

    # Deterministic safety net first: never let a losing position ride to the
    # full broker SL or sit through a clear HTF trend flip.
    exit_reason = _mt5_forced_exit_reason(cfg, pos, price, trend_klines, entry_klines)
    if exit_reason:
        mt5_venue.mt5_close_positions(symbol)
        append_trade(
            {"status": "manage_exit", "venue": "mt5", "symbol": symbol,
             "decision": {"action": "EXIT", "new_stop_loss": None,
                          "confidence": 1.0, "reason": exit_reason},
             "position": pos, "forced": True}
        )
        notify(
            f"🛑 `{name}:{symbol}` SAFETY EXIT {pos['side']} {pos['quantity']:.2f} lots "
            f"@ {price:.5f} — {exit_reason}"
        )
        return

    prompt = _mt5_manage_prompt(cfg, symbol, pos, trend_klines, entry_klines)
    raw = ask_deepseek(prompt, fast=cfg["fast_llm"])
    log(f"DeepSeek manage raw response ({symbol}): {raw[:500]}")

    decision = parse_decision(raw)
    log(
        f"Manage decision ({symbol}): action={decision.get('action')} "
        f"new_sl={decision.get('new_stop_loss')} conf={decision.get('confidence')}"
    )

    entry_atr = atr(entry_klines)
    min_trail_dist = cfg.get("trail_min_atr", 0.5) * entry_atr
    ok, reason = validate_manage_decision(decision, pos, min_trail_dist=min_trail_dist)
    if not ok:
        log(f"MANAGE SKIPPED ({symbol}): {reason}")
        append_trade(
            {"status": "manage_hold", "venue": "mt5", "symbol": symbol,
             "reason": reason, "decision": decision, "position": pos}
        )
        notify(f"🤖 `{name}:{symbol}` position: {reason[:220]}")
        return

    action = decision["action"]
    if action == "EXIT":
        mt5_venue.mt5_close_positions(symbol)
        append_trade(
            {"status": "manage_exit", "venue": "mt5", "symbol": symbol,
             "decision": decision, "position": pos}
        )
        notify(
            f"🚪 `{name}:{symbol}`: LLM EXIT {pos['side']} {pos['quantity']:.2f} lots "
            f"@ {price:.5f} — {str(decision.get('reason', ''))[:160]}"
        )
        return

    # TRAIL — modify the live position's stop (keep take-profit unchanged).
    new_sl = float(decision["new_stop_loss"])
    old_sl = pos["sl"]
    tp = pos["tp"]
    mt5_venue.mt5_modify_position(pos["ticket"], new_sl, tp)
    append_trade(
        {"status": "manage_trail", "venue": "mt5", "symbol": symbol,
         "ticket": pos["ticket"], "new_stop_loss": new_sl, "old_stop_loss": old_sl,
         "decision": decision, "position": pos}
    )
    notify(
        f"🪝 `{name}:{symbol}`: LLM TRAIL SL {old_sl or 'none'} → {new_sl:.5f} "
        f"({pos['side']} {pos['quantity']:.2f} lots) — {str(decision.get('reason', ''))[:160]}"
    )


# ---------------------------------------------------------------------------
# MT5 cycle — one pass per symbol, atomic SL+TP protection
# ---------------------------------------------------------------------------
def run_mt5_cycle(name: str, cfg: dict) -> None:
    """Run one full MT5 cycle: connect once, then one decision per symbol.
    Never raises — mirrors the Binance path's safety so the loop keeps running."""
    try:
        mt5_venue.mt5_ensure_ready()
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR in MT5 cycle: {exc}")
        append_trade({"status": "error", "error": str(exc), "venue": "mt5"})
        notify(f"⚠️ `{name}` MT5 cycle error: {exc}")
        return
    for symbol in cfg["symbols"]:
        try:
            run_mt5_symbol(name, cfg, symbol)
        except Exception as exc:  # noqa: BLE001 — keep going to the next symbol
            log(f"ERROR in MT5 cycle ({symbol}): {exc}")
            append_trade({"status": "error", "error": str(exc), "symbol": symbol, "venue": "mt5"})
            notify(f"⚠️ `{name}:{symbol}` cycle error: {exc}")


def run_mt5_symbol(name: str, cfg: dict, symbol: str) -> None:
    """Decision + entry + atomic SL/TP protection for one MT5 symbol."""
    log(
        f"── MT5 cycle ── strategy={name} symbol={symbol} "
        f"trend={cfg['interval']} entry={cfg['entry_interval']}"
    )

    trend_klines = mt5_venue.mt5_fetch_klines(symbol, cfg["interval"], cfg["fetch_limit"])
    entry_klines = mt5_venue.mt5_fetch_klines(symbol, cfg["entry_interval"], cfg["fetch_limit"])
    price = float(entry_klines[-1]["close"])
    log(
        f"Fetched {len(trend_klines)} {cfg['interval']} / {len(entry_klines)} "
        f"{cfg['entry_interval']} candles, last={price:.5f}"
    )

    # One position per symbol — never stack. An open position is managed by the LLM.
    positions = mt5_venue.mt5_positions(symbol)
    if positions:
        if POSITION_MANAGER:
            manage_mt5_position(name, cfg, symbol, positions[0],
                                trend_klines, entry_klines, price)
        else:
            n = len(positions)
            msg = f"⏭ `{name}:{symbol}`: {n} open position(s). Skipping entry."
            log(msg)
            notify(msg)
        return

    prompt = build_mt5_prompt(symbol, cfg, trend_klines, entry_klines)
    raw = ask_deepseek(prompt, fast=cfg["fast_llm"])
    log(f"DeepSeek raw response: {raw[:500]}")

    decision = parse_decision(raw)
    log(
        f"Decision: action={decision.get('action')} lots={decision.get('quantity')} "
        f"sl={decision.get('stop_loss')} tp={decision.get('take_profit')} "
        f"conf={decision.get('confidence')}"
    )

    entry_closes = [float(k["close"]) for k in entry_klines]
    entry_rsi7 = rsi(entry_closes, 7)
    entry_vol = volume_ratio(entry_klines)
    entry_atr = atr(entry_klines)
    ok, reason = validate_mt5_decision(decision, price, symbol, cfg,
                                       entry_rsi7=entry_rsi7, entry_vol=entry_vol,
                                       entry_atr=entry_atr)
    if not ok:
        log(f"SKIPPED: {reason}")
        append_trade(
            {
                "status": "rejected",
                "reason": reason,
                "decision": decision,
                "price": price,
                "symbol": symbol,
                "venue": "mt5",
            }
        )
        notify(reject_message(f"{name}:{symbol}", decision, price, reason))
        return

    # Stacking guard: a position may have appeared since the decision (or the
    # earlier snapshot was stale) — re-check right before ordering.
    if mt5_venue.mt5_positions(symbol):
        log(f"SKIPPED: {symbol} now has an open position — no stacking.")
        append_trade(
            {"status": "rejected",
             "reason": "Skipped: a position opened since the decision (stacking guard)",
             "decision": decision, "price": price, "symbol": symbol, "venue": "mt5"}
        )
        return

    result = mt5_venue.mt5_market_order(
        symbol,
        decision["action"],
        float(decision["quantity"]),
        float(decision["stop_loss"]),
        float(decision.get("take_profit") or 0),
    )
    filled_lots = result["volume"]
    protection = "SL+TP (atomic)" if decision.get("take_profit") else "SL (atomic)"
    append_trade(
        {
            "status": "filled",
            "venue": "mt5",
            "symbol": symbol,
            "decision": decision,
            "price": price,
            "protection": protection,
            "order": result,
        }
    )
    notify(
        f"✅ `{name}:{symbol}` FILLED {decision['action']} {filled_lots:.2f} lots @ ${price:,.5f} "
        f"| {protection}"
    )


# ---------------------------------------------------------------------------
# One full cycle
# ---------------------------------------------------------------------------
def run_once(name: str = None) -> None:
    """Execute one full cycle for a strategy: position check → decide → guardrail → entry+protect → log."""
    name = name or CURRENT_STRATEGY
    if PAUSED:
        log(f"Paused — skipping {name} cycle.")
        return
    cfg = STRATEGIES[name]
    if cfg.get("venue") == "mt5":
        try:
            run_mt5_cycle(name, cfg)
        except Exception as exc:  # noqa: BLE001 — never crash the schedule loop
            log(f"ERROR in MT5 cycle: {exc}")
        return
    log(f"── Cycle start ── strategy={name} symbol={SYMBOL} interval={cfg['interval']} candles={cfg['candles']}")
    try:
        klines = fetch_klines(cfg)
        price = float(klines[-1]["close"])
        log(f"Fetched {len(klines)} candles, last close=${price:.2f}")

        # ── Position management: one protected position at a time ──
        open_orders = get_open_orders()
        base_balance = get_base_balance()
        position_value = base_balance * price
        if open_orders or position_value >= MIN_NOTIONAL_USD:
            if open_orders and position_value < MIN_NOTIONAL_USD:
                # Position already closed by SL/TP — clean up stale orders
                cancel_open_orders()
                clear_tracked_binance_position()
                log("Position already closed; cleaned up stale protective orders.")
                notify(f"🧹 `{name}`: stale protective orders cleaned up.")
            elif not open_orders:
                # Unmanaged/naked position: base balance exists but there are NO
                # protective orders. We don't know its entry (not created this run),
                # so we can't attach a correct SL/TP — close it so the bot never
                # leaves a position unprotected and never stacks on top of it.
                log(
                    f"⚠️ UNPROTECTED position: {base_balance:.6f} {BASE_ASSET} "
                    f"(≈${position_value:.2f}) with 0 protective orders — safety-closing."
                )
                notify(
                    f"🛑 `{name}`: unprotected position (≈${position_value:.2f}, "
                    f"0 protective orders) — safety-closing to stay flat."
                )
                safety_close_position("SELL", base_balance)  # spot long → sell to close
                clear_tracked_binance_position()
            else:
                if POSITION_MANAGER:
                    # LLM analyzes the protected open position (HOLD/TRAIL/EXIT)
                    manage_binance_position(name, cfg, klines, open_orders, base_balance)
                else:
                    msg = (
                        f"⏭ `{name}`: position open ({len(open_orders)} protective order(s), "
                        f"base={base_balance:.6f}). Skipping entry."
                    )
                    log(msg)
                    notify(msg)
            return

        # ── AI decision ──
        prompt = build_prompt(klines, name)
        raw = ask_deepseek(prompt, fast=cfg["fast_llm"])
        log(f"DeepSeek raw response: {raw[:500]}")

        decision = parse_decision(raw)
        log(
            f"Decision: action={decision.get('action')} qty={decision.get('quantity')} "
            f"sl={decision.get('stop_loss')} tp={decision.get('take_profit')} "
            f"conf={decision.get('confidence')}"
        )

        ok, reason = validate_decision(decision, price)
        if not ok:
            log(f"SKIPPED: {reason}")
            append_trade(
                {
                    "status": "rejected",
                    "reason": reason,
                    "decision": decision,
                    "price": price,
                }
            )
            notify(reject_message(name, decision, price, reason))
            return

        # ── Entry, then IMMEDIATE protection (the Samuel Phase E fix) ──
        order = place_market_order(decision, price)
        filled_qty = float(order.get("executedQty") or float(decision["quantity"]))
        try:
            protection = place_protective_orders(decision, filled_qty)
        except Exception as exc:  # noqa: BLE001
            log(f"CRITICAL: protective order failed ({exc}); closing for safety.")
            safety_close_position(opposite_side(decision["action"]), filled_qty)
            raise

        # Track the position so the LLM manager knows its entry price/PnL.
        fill_price = price
        try:
            fills = order.get("fills") or []
            if fills:
                fill_price = float(fills[0].get("price", price))
        except (TypeError, ValueError):
            pass
        track_binance_position(name, decision, filled_qty, fill_price, protection)

        append_trade(
            {
                "status": "filled",
                "decision": decision,
                "price": price,
                "protection": protection,
                "order": {
                    k: order.get(k)
                    for k in (
                        "orderId",
                        "clientOrderId",
                        "status",
                        "side",
                        "type",
                        "executedQty",
                        "cummulativeQuoteQty",
                    )
                },
            }
        )
        notify(
            f"✅ `{name}` FILLED {decision['action']} {filled_qty:.6f} {SYMBOL} @ ${price:,.2f} "
            f"| {protection}"
        )
    except Exception as exc:  # noqa: BLE001 — keep the loop alive
        log(f"ERROR in cycle: {exc}")
        append_trade({"status": "error", "error": str(exc)})
        notify(f"⚠️ `{name}` cycle error: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    global CURRENT_STRATEGY, TELEGRAM_ADMIN_CHAT, PAUSED, TRACKED_POSITION
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log("Binance DeepSeek bot starting (DEMO MODE)")

    # Restore persisted runtime state (strategy switch / admin chat survive restarts)
    state = load_state()
    if state.get("strategy") in STRATEGIES:
        CURRENT_STRATEGY = state["strategy"]
    if state.get("telegram_chat_id"):
        TELEGRAM_ADMIN_CHAT = state["telegram_chat_id"]
    PAUSED = bool(state.get("paused", False))
    TRACKED_POSITION = state.get("tracked_position")

    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        log("ERROR: BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET missing in .env")
        sys.exit(1)
    if not DEEPSEEK_API_KEY:
        log("ERROR: DEEPSEEK_API_KEY missing in .env")
        sys.exit(1)
    if not verify_credentials():
        log(
            "No valid Binance Demo credentials — the bot is going idle. "
            "Fix BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET in .env "
            "then run: docker compose restart bot"
        )
        # Do NOT exit: `restart: unless-stopped` restarts on any exit code,
        # which would cause a crash-loop. Stay idle until .env is fixed.
        while True:
            time.sleep(3600)

    if TELEGRAM_BOT_TOKEN:
        log("Telegram control enabled.")
        notify(
            f"🚀 *Bot online* — strategy `{CURRENT_STRATEGY}`\n"
            f"{strategy_line(CURRENT_STRATEGY)}\n"
            "Send /help for commands."
        )
    else:
        log("No TELEGRAM_BOT_TOKEN — running without Telegram control.")

    # Run once immediately, then on schedule
    run_once(CURRENT_STRATEGY)
    schedule_strategy(CURRENT_STRATEGY)
    log("Entering loop (Ctrl+C to stop).")

    offset = 0
    while True:
        # 1. Telegram long-poll (blocks up to ~15s; updates queue server-side meanwhile)
        if TELEGRAM_BOT_TOKEN:
            for u in tg_get_updates(offset):
                handle_update(u)
                offset = u.get("update_id", offset) + 1
        # 2. Run any due cycles
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
