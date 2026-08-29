"""
MetaTrader 5 venue adapter — ALL MetaTrader5-specific code lives here.

The Binance venue code in bot.py is untouched; the 'mt5' strategy routes through
these functions.

Connection: the MT5 terminal runs under WINE inside the same container, and a
small bridge process (mt5_bridge.py, running under WINE Python with the official
Windows `MetaTrader5` package) exposes a tiny HTTP API. This module is the
Linux-side HTTP client for that bridge. If the bridge is unreachable, calling an
MT5 function raises a clear MT5Error.
"""

import os

import requests

# Env config for the MT5 venue (read at import — bot.py loads .env first).
MT5_LOGIN = os.getenv("MT5_LOGIN", "")
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")
MT5_MAGIC = int(os.getenv("MT5_MAGIC", "424242"))
MT5_BRIDGE_URL = os.getenv("MT5_BRIDGE_URL", "http://localhost:18080").rstrip("/")
MT5_SYMBOLS = [
    s.strip()
    for s in os.getenv("MT5_SYMBOLS", "XAUUSD,EURUSD").split(",")
    if s.strip()
]

MT5_AVAILABLE = True  # the HTTP bridge is always callable; unreachable => MT5Error


class MT5Error(RuntimeError):
    """Raised when the MT5 bridge is unreachable or an MT5 operation fails."""


def _req(method: str, path: str, **kw):
    """Call the MT5 HTTP bridge and return its JSON (raising MT5Error on failure)."""
    try:
        resp = requests.request(method, MT5_BRIDGE_URL + path, timeout=60, **kw)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not resp.ok:
            # Surface the bridge's own error body (e.g. "mt5.login() failed:
            # (10024, ...)") instead of an opaque "500 Server Error" — that body
            # is exactly what tells you whether the terminal or the account is
            # the problem.
            detail = data.get("error", "") if isinstance(data, dict) else ""
            raise MT5Error(f"MT5 bridge error {resp.status_code}: {detail}")
    except requests.RequestException as exc:
        raise MT5Error(f"MT5 bridge unreachable at {MT5_BRIDGE_URL}: {exc}")
    if isinstance(data, dict) and "error" in data:
        raise MT5Error(
            f"MT5 bridge error: {data['error']} "
            f"(last_error={data.get('last_error')})"
        )
    return data


def _obj(data: dict):
    """Turn a bridge JSON dict into a lightweight object with attribute access."""
    return type("MT5Obj", (), dict(data or {}))


# ---------------------------------------------------------------------------
# Connection / account
# ---------------------------------------------------------------------------
def mt5_ensure_ready() -> None:
    """Initialize the connection and (re)log into the configured demo account.
    Safe to call on every cycle."""
    _req("GET", "/health")  # raises MT5Error if the bridge/terminal is down


def mt5_account_info():
    return _obj(_req("GET", "/account"))


def mt5_account_balance() -> float:
    return float(getattr(mt5_account_info(), "balance", 0.0))


def mt5_is_demo() -> bool:
    """Return True if the connected account is a DEMO account."""
    return bool(_req("GET", "/health").get("demo", False))


def mt5_symbol_info(symbol: str):
    """Return the terminal's symbol info (contract size, lot bounds, digits...)."""
    return _obj(_req("GET", "/symbol_info", params={"symbol": symbol}))


def mt5_health_check() -> str:
    """Short human-readable health summary (used by /status and startup)."""
    try:
        h = _req("GET", "/health")
        mode = "DEMO" if h.get("demo") else "REAL?"
        lines = [
            f"MT5 connected: {h.get('login', '?')}@{h.get('server', '?')} ({mode})",
            f"Balance: {h.get('currency', '')} {h.get('balance', 0.0):,.2f} | "
            f"equity {h.get('equity', 0.0):,.2f}",
        ]
        for sym in MT5_SYMBOLS:
            info = _req("GET", "/symbol_info", params={"symbol": sym})
            tk = _req("GET", "/tick", params={"symbol": sym})
            lines.append(
                f"  {sym}: last={tk.get('bid')} "
                f"| contract={info.get('trade_contract_size')} "
                f"lot {info.get('volume_min')}–{info.get('volume_max')}"
            )
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        return f"MT5 unavailable: {exc}"


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
def mt5_fetch_klines(symbol: str, interval: str, count: int) -> list:
    """Fetch the latest candles for a symbol/timeframe in the shared kline shape."""
    data = _req(
        "GET",
        "/klines",
        params={"symbol": symbol, "timeframe": interval, "count": count},
    )
    return [
        {
            "open_time": int(r["time"]) * 1000,  # bridge returns seconds
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r["tick_volume"]),
            "quote_volume": None,  # prefer tick_volume
        }
        for r in data
    ]


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------
def mt5_positions(symbol: str = None) -> list:
    params = {"symbol": symbol} if symbol else {}
    return [_obj(p) for p in _req("GET", "/positions", params=params)]


def mt5_close_positions(symbol: str = None) -> None:
    """Best-effort market-close of every open position (safety)."""
    params = {"symbol": symbol} if symbol else {}
    _req("POST", "/close", params=params)


def mt5_modify_position(ticket, sl: float, tp: float = 0.0) -> dict:
    """Move an open position's SL/TP (used by the LLM position manager TRAIL)."""
    return _req(
        "POST",
        "/modify",
        json={"ticket": int(ticket), "sl": float(sl), "tp": float(tp)},
    )


# ---------------------------------------------------------------------------
# Order execution (atomic SL+TP — MT5 has no OCO concept)
# ---------------------------------------------------------------------------
def mt5_market_order(symbol: str, action: str, lots: float, stop_loss: float,
                     take_profit: float = 0.0) -> dict:
    """Place a market order with SL+TP attached atomically (never unprotected)."""
    return _req(
        "POST",
        "/order",
        json={
            "symbol": symbol,
            "action": action,
            "lots": lots,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        },
    )
