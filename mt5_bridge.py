"""
MT5 HTTP bridge — runs under WINE PYTHON using the OFFICIAL Windows MetaTrader5
package, next to the MT5 terminal in the same Wine prefix.

This is the Linux story: the `MetaTrader5` pip package is Windows-only, so a
small Windows-Python process (running under Wine) hosts the real MetaTrader5
module and exposes a tiny HTTP API. The Linux bot (mt5_venue.py) talks to it
over plain HTTP — no third-party bridge binaries, no rpyc protocol to match.

Start (entrypoint):
    cd /app && wine "C:\\Python311\\python.exe" mt5_bridge.py
Env (from the container): MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_TERMINAL_PATH.

Endpoints:
    GET  /health        -> {login, server, balance, equity, currency, demo}
    GET  /account       -> {login, server, balance, equity, currency, trade_mode}
    GET  /symbol_info   -> ?symbol=    (contract size, lot bounds, digits, point, ...)
    GET  /tick          -> ?symbol=    {bid, ask}
    GET  /klines        -> ?symbol=&timeframe=&count=
    GET  /positions     -> ?symbol=    (optional)
    POST /order         -> JSON {symbol, action, lots, stop_loss, take_profit}
    POST /modify        -> JSON {ticket, sl, tp}   (move an open position's SL/TP)
    POST /close         -> ?symbol=    (close all positions for a symbol)
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import MetaTrader5 as mt5
except Exception as exc:  # noqa: BLE001
    print(f"[bridge] FATAL: MetaTrader5 not importable under Wine Python: {exc}", flush=True)
    sys.exit(1)

TIMEFRAMES = {
    "m1": mt5.TIMEFRAME_M1, "m5": mt5.TIMEFRAME_M5, "m15": mt5.TIMEFRAME_M15,
    "m30": mt5.TIMEFRAME_M30, "h1": mt5.TIMEFRAME_H1, "h4": mt5.TIMEFRAME_H4,
    "d1": mt5.TIMEFRAME_D1,
}


# Serialize ALL access to the terminal: concurrent mt5.initialize()/login() calls
# against the same Wine terminal can collide and hang one of them indefinitely.
_MT5_LOCK = threading.Lock()
# time.monotonic() when a request acquired the lock (0.0 = lock is free).
_LOCK_HELD_AT = 0.0
# Hard cap so a slow/busy terminal returns an error instead of hanging a request.
_INIT_TIMEOUT_MS = 20000
# If a request holds the lock longer than this, the terminal is wedged (Wine can
# ignore the initialize() timeout while a modal login dialog is up). Fail new
# requests fast instead of queueing more threads behind a hung Wine process.
_BUSY_TIMEOUT_S = 60.0


def _find_terminal():
    """Locate terminal64.exe (Windows paths valid inside the Wine prefix)."""
    candidates = [
        os.environ.get("MT5_TERMINAL_PATH", ""),
        r"C:\Program Files\MetaTrader 5\terminal64.exe",
        r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


def _initialize():
    """Attach to the terminal with a hard timeout (never hang forever).

    Credentials are passed straight to initialize(): on a fresh container the
    terminal often sits on the login screen (no auto-login), and plain
    initialize() blocks on the modal dialog. initialize(login=..., password=...)
    performs the login AS PART of attaching, so it works even from that state.
    """
    login = os.environ.get("MT5_LOGIN", "")
    pwd = os.environ.get("MT5_PASSWORD", "")
    srv = os.environ.get("MT5_SERVER", "")
    kw = {"timeout": _INIT_TIMEOUT_MS}
    if login and pwd:
        kw.update({"login": int(login), "password": pwd, "server": srv or None})
    if mt5.initialize(**kw):
        return
    term = _find_terminal()
    if term and mt5.initialize(path=term, **kw):
        return
    raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")


def _login():
    login = os.environ.get("MT5_LOGIN", "")
    pwd = os.environ.get("MT5_PASSWORD", "")
    srv = os.environ.get("MT5_SERVER", "")
    if login and pwd:
        acc = mt5.account_info()
        if acc is None or str(acc.login) != str(login):
            if not mt5.login(int(login), pwd, server=srv or None):
                raise RuntimeError(f"mt5.login() failed: {mt5.last_error()}")


def _ensure():
    """Serialized initialize + login — the only entry point handlers use.

    Fails fast when the terminal is wedged: if a previous request has been stuck
    holding the lock for a long time (Wine can ignore the initialize() timeout
    while a modal login dialog is up), new requests return a clear error instead
    of blocking their HTTP thread indefinitely — which previously surfaced to the
    Linux bot as a 60s read timeout.
    """
    global _LOCK_HELD_AT
    if _LOCK_HELD_AT and time.monotonic() - _LOCK_HELD_AT > _BUSY_TIMEOUT_S:
        raise RuntimeError("MT5 terminal wedged (request held too long) — retry shortly")
    if not _MT5_LOCK.acquire(timeout=_INIT_TIMEOUT_MS / 1000 + 5):
        raise RuntimeError("MT5 terminal busy (another request in flight)")
    try:
        _LOCK_HELD_AT = time.monotonic()
        _initialize()
        _login()
    finally:
        _LOCK_HELD_AT = 0.0
        _MT5_LOCK.release()


def _watchdog():
    """Force-exit the process if a request is stuck on the terminal too long.

    Wine can ignore mt5.initialize()'s timeout while a modal login dialog is up,
    which would otherwise wedge the bridge forever (the stuck thread holds the
    lock). Force-exit so the entrypoint's supervisor restarts us fresh — each
    restart gives the terminal another chance to authorize, and the Linux bot
    just sees a brief "unreachable" instead of permanent 60s read timeouts.
    """
    while True:
        time.sleep(max(5.0, _BUSY_TIMEOUT_S / 2))
        if _LOCK_HELD_AT and time.monotonic() - _LOCK_HELD_AT > _BUSY_TIMEOUT_S:
            print(
                f"[bridge] WATCHDOG: terminal wedged >{_BUSY_TIMEOUT_S:.0f}s "
                "(initialize/login stuck) — forcing restart",
                flush=True,
            )
            os._exit(1)


def _filling_mode(symbol):
    mode = int(getattr(mt5.symbol_info(symbol), "filling_mode", 0) or 0)
    if mode & 2:
        return mt5.ORDER_FILLING_IOC
    if mode & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _account_dict(acc):
    return {
        "login": str(getattr(acc, "login", "?")),
        "server": str(getattr(acc, "server", "?")),
        "balance": float(getattr(acc, "balance", 0.0)),
        "equity": float(getattr(acc, "equity", 0.0)),
        "currency": str(getattr(acc, "currency", "")),
        "trade_mode": int(getattr(acc, "trade_mode", -1)),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def health():
    _ensure()
    acc = mt5.account_info()
    if acc is None:
        raise RuntimeError(f"account_info failed: {mt5.last_error()}")
    d = _account_dict(acc)
    # ACCOUNT_TRADE_MODE_DEMO == 0 in the native module
    d["demo"] = bool(getattr(acc, "trade_mode", -1) == 0)
    return d


def account():
    _ensure()
    acc = mt5.account_info()
    if acc is None:
        raise RuntimeError(f"account_info failed: {mt5.last_error()}")
    return _account_dict(acc)


def symbol_info(symbol):
    _ensure()
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"symbol_info({symbol}) failed: {mt5.last_error()}")
    if not getattr(info, "visible", True):
        try:
            mt5.symbol_select(symbol, True)
        except Exception:  # noqa: BLE001
            pass
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"symbol {symbol!r} not found")
    return {
        "trade_contract_size": float(getattr(info, "trade_contract_size", 1.0) or 1.0),
        "volume_min": float(getattr(info, "volume_min", 0.01) or 0.01),
        "volume_max": float(getattr(info, "volume_max", 1.0) or 1.0),
        "volume_step": float(getattr(info, "volume_step", 0.01) or 0.01),
        "digits": int(getattr(info, "digits", 2)),
        "point": float(getattr(info, "point", 0.01) or 0.01),
        "filling_mode": int(getattr(info, "filling_mode", 0) or 0),
    }


def tick(symbol):
    _ensure()
    t = mt5.symbol_info_tick(symbol)
    if t is None:
        raise RuntimeError(f"symbol_info_tick({symbol}) failed: {mt5.last_error()}")
    return {"bid": float(getattr(t, "bid", 0.0)), "ask": float(getattr(t, "ask", 0.0))}


def klines(symbol, timeframe, count):
    _ensure()
    tf = TIMEFRAMES.get(timeframe.lower())
    if tf is None:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, int(count))
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"copy_rates_from_pos({symbol},{timeframe}) failed: {mt5.last_error()}")
    return [
        {
            "time": int(r["time"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "tick_volume": float(r["tick_volume"]),
        }
        for r in rates
    ]


def positions(symbol=None):
    _ensure()
    if symbol:
        pos = mt5.positions_get(symbol=symbol)
    else:
        pos = mt5.positions_get()
    return [
        {
            "ticket": int(getattr(p, "ticket", 0)),
            "symbol": str(getattr(p, "symbol", "")),
            "type": int(getattr(p, "type", 0)),
            "volume": float(getattr(p, "volume", 0.0)),
            "price_open": float(getattr(p, "price_open", 0.0)),
            "price_current": float(getattr(p, "price_current", 0.0)),
            "sl": float(getattr(p, "sl", 0.0)),
            "tp": float(getattr(p, "tp", 0.0)),
            "profit": float(getattr(p, "profit", 0.0)),
        }
        for p in (pos or [])
    ]


def place_order(body):
    _ensure()
    symbol = body["symbol"]
    action = str(body["action"]).upper()
    lots = float(body["lots"])
    sl = float(body.get("stop_loss", 0.0) or 0.0)
    tp = float(body.get("take_profit", 0.0) or 0.0)

    t = mt5.symbol_info_tick(symbol)
    if t is None:
        raise RuntimeError(f"symbol_info_tick({symbol}) failed: {mt5.last_error()}")
    if action == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = getattr(t, "ask", 0.0)
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price = getattr(t, "bid", 0.0)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lots,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": int(os.environ.get("MT5_MAGIC", "424242")),
        "comment": "deepseek-bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _filling_mode(symbol),
    }
    result = mt5.order_send(request)
    if result is None:
        raise RuntimeError(f"order_send failed: {mt5.last_error()}")
    if getattr(result, "retcode", None) != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(
            f"order_send rejected retcode={getattr(result, 'retcode', '?')}: "
            f"{getattr(result, 'comment', '')}"
        )
    return {
        "order": int(getattr(result, "order", 0)),
        "deal": int(getattr(result, "deal", 0)),
        "price": float(getattr(result, "price", 0.0)),
        "volume": float(getattr(result, "volume", lots)),
        "retcode": int(getattr(result, "retcode", -1)),
        "comment": str(getattr(result, "comment", "")),
    }


def modify_position(body):
    _ensure()
    ticket = int(body["ticket"])
    sl = float(body.get("sl", 0.0) or 0.0)
    tp = float(body.get("tp", 0.0) or 0.0)
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl": sl,
        "tp": tp,
    }
    result = mt5.order_send(request)
    if result is None:
        raise RuntimeError(f"order_send(modify) failed: {mt5.last_error()}")
    if getattr(result, "retcode", None) != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(
            f"modify rejected retcode={getattr(result, 'retcode', '?')}: "
            f"{getattr(result, 'comment', '')}"
        )
    return {
        "ticket": ticket,
        "sl": sl,
        "tp": tp,
        "retcode": int(getattr(result, "retcode", -1)),
        "comment": str(getattr(result, "comment", "")),
    }


def close_positions(symbol=None):
    _ensure()
    pos = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    closed = []
    for p in (pos or []):
        t = mt5.symbol_info_tick(p.symbol)
        if t is None:
            raise RuntimeError(f"symbol_info_tick({p.symbol}) failed")
        order_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = getattr(t, "bid", 0.0) if order_type == mt5.ORDER_TYPE_SELL else getattr(t, "ask", 0.0)
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "position": p.ticket,
            "volume": float(p.volume),
            "type": order_type,
            "price": price,
            "deviation": 20,
            "magic": int(os.environ.get("MT5_MAGIC", "424242")),
            "comment": "safety-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _filling_mode(p.symbol),
        }
        r = mt5.order_send(req)
        if r is None or getattr(r, "retcode", None) != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"close {p.ticket} failed: {mt5.last_error()}")
        closed.append(int(p.ticket))
    return {"closed": closed}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _send(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _run(self, fn):
        try:
            self._send(fn())
        except Exception as exc:  # noqa: BLE001
            try:
                le = list(mt5.last_error())
            except Exception:  # noqa: BLE001
                le = []
            self._send({"error": str(exc), "last_error": le}, status=500)

    def do_GET(self):
        parts = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parts.query)
        g = lambda k, d=None: (q.get(k) or [d])[0]
        path = parts.path
        if path == "/health":
            self._run(health)
        elif path == "/account":
            self._run(account)
        elif path == "/symbol_info":
            self._run(lambda: symbol_info(g("symbol", "")))
        elif path == "/tick":
            self._run(lambda: tick(g("symbol", "")))
        elif path == "/klines":
            self._run(lambda: klines(g("symbol", ""), g("timeframe", "m15"), int(g("count", "300"))))
        elif path == "/positions":
            sym = g("symbol", None)
            self._run(lambda: positions(sym))
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        parts = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parts.query)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        if parts.path == "/order":
            self._run(lambda: place_order(body))
        elif parts.path == "/modify":
            self._run(lambda: modify_position(body))
        elif parts.path == "/close":
            sym = (q.get("symbol") or [None])[0]
            self._run(lambda: close_positions(sym))
        else:
            self._send({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MT5_HTTP_PORT", "18080")))
    args = ap.parse_args()
    # Watchdog: if the Wine terminal wedges, force-exit so the supervisor restarts us.
    threading.Thread(target=_watchdog, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[bridge] MT5 bridge listening on {args.host}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
