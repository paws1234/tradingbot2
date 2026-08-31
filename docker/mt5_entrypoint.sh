#!/usr/bin/env bash
# Entrypoint for the MT5-capable bot image (Dockerfile.mt5) — Layout B.
# EVERYTHING runs in this container:
#   Xvfb + VNC (view/operate the terminal) + Windows Python (installed under
#   Wine on first run) + the MT5 terminal + mt5_bridge.py (Wine-Python HTTP
#   bridge using the official MetaTrader5 package) + the bot.
#
# One-time demo login: open VNC at localhost:5901 (no password) and log the MT5
# terminal into your demo account, or let the bridge auto-login via MT5_* env.
set -e

export DISPLAY=:99
export WINEDEBUG=-all

# 1. Virtual display for Wine (idempotent — survives docker restart).
# An unclean stop (docker stop / OOM kill) leaves a stale /tmp/.X99-lock and a
# dead /tmp/.X11-unix/X99 socket. Xvfb then refuses to start on :99, and because
# the MT5 terminal cannot create a window without a live display it exits within
# a second — the bridge then fails mt5.initialize() (IPC timeout / send failed),
# its watchdog force-exits it, the supervisor restarts it, and the whole stack
# crash-loops (bot logs "MT5 bridge unreachable" every cycle). Clear stale
# artifacts and VERIFY the display answers instead of trusting a pgrep.
if ! xdpyinfo -display :99 >/dev/null 2>&1; then
  echo "[mt5] Starting Xvfb on :99..."
  rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
  Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
  for _ in $(seq 1 10); do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
      echo "[mt5] Xvfb ready on :99."
      break
    fi
    sleep 1
  done
  if ! xdpyinfo -display :99 >/dev/null 2>&1; then
    echo "[mt5] WARNING: Xvfb failed on :99 — MT5 terminal will not start. tail /tmp/xvfb.log:"
    tail -5 /tmp/xvfb.log 2>/dev/null || true
  fi
fi

# 2. VNC so you can see/operate the MT5 terminal (one-time demo login).
# NOTE: -rfbport 5901 is required — x11vnc's default is 5900, which breaks the
# 5901 port mapping and websockify's localhost:5901 target.
if ! pgrep -f "x11vnc" >/dev/null 2>&1; then
  x11vnc -display :99 -rfbport 5901 -forever -nopw -shared -quiet >/dev/null 2>&1 &
  echo "[mt5] VNC ready on localhost:5901 (no password)."
fi

# 2b. noVNC — open the MT5 terminal in a BROWSER (no VNC client needed):
#     http://localhost:6080/vnc.html  (from another machine use this host's IP)
if ! pgrep -f "websockify" >/dev/null 2>&1; then
  (websockify 0.0.0.0:6080 127.0.0.1:5901 --web=/usr/share/novnc >/tmp/novnc.log 2>&1 &)
  sleep 2
  echo "[mt5] noVNC ready at http://localhost:6080/vnc.html"
fi

WPY='C:\Python312\python.exe'
WPY_LINUX='/root/.wine/drive_c/Python312/python.exe'

# 3. Windows Python (64-bit embeddable — no 32-bit NSIS stub) + pip, one-time.
if [ ! -f "$WPY_LINUX" ]; then
  echo "[mt5] Installing Windows Python (embeddable) under Wine (one-time)..."
  mkdir -p /root/.wine/drive_c/Python312
  if [ ! -f /opt/python-embed.zip ]; then
    echo "[mt5] Downloading Windows Python embeddable zip..."
    wget -q "https://www.python.org/ftp/python/3.12.4/python-3.12.4-embed-amd64.zip" \
      -O /opt/python-embed.zip || echo "[mt5] WARNING: python embed download failed"
  fi
  if [ -f /opt/python-embed.zip ]; then
    unzip -oq /opt/python-embed.zip -d /root/.wine/drive_c/Python312 || true
    # enable pip: uncomment "import site" in the ._pth file
    sed -i 's/^#import site/import site/' /root/.wine/drive_c/Python312/python312._pth || true
    wget -q "https://bootstrap.pypa.io/get-pip.py" \
      -O /root/.wine/drive_c/Python312/get-pip.py || true
    (cd /root/.wine/drive_c/Python312 && timeout 300 wine "$WPY" get-pip.py) \
      || echo "[mt5] WARNING: get-pip returned non-zero"
  fi
fi

# 4. Official MetaTrader5 package into Wine Python. Pin numpy<2: numpy 2.x calls
#    ucrtbase.dll.crealf which this Wine build doesn't implement (bridge crash).
if [ -f "$WPY_LINUX" ]; then
  echo "[mt5] Ensuring MetaTrader5 + numpy<2 in Wine Python..."
  wine "$WPY" -m pip install --no-cache-dir --upgrade "numpy<2" MetaTrader5 \
    || echo "[mt5] WARNING: MetaTrader5 pip install failed"
else
  echo "[mt5] WARNING: Windows Python not installed."
fi

# 5. MT5 terminal under Wine (one-time).
TERMINAL="$(find /root/.wine -name terminal64.exe -path '*MetaTrader*' ! -path '*.broken*' 2>/dev/null | head -n1 || true)"
if [ -z "$TERMINAL" ]; then
  echo "[mt5] Installing MT5 terminal (one-time, may take a few minutes)..."
  if [ ! -f /opt/mt5setup.exe ]; then
    echo "[mt5] Downloading MetaTrader 5 installer..."
    wget -q "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" \
      -O /opt/mt5setup.exe || echo "[mt5] WARNING: installer download failed"
  fi
  if [ -f /opt/mt5setup.exe ]; then
    timeout 600 wine /opt/mt5setup.exe /auto >/dev/null 2>&1 || \
      echo "[mt5] installer returned non-zero — if a setup window opened, finish it over VNC"
    TERMINAL="$(find /root/.wine -name terminal64.exe -path '*MetaTrader*' ! -path '*.broken*' 2>/dev/null | head -n1 || true)"
  fi
fi

# 5b. Force the terminal config to allow algo trading + Python API on every boot.
#     `[Experts] Api=1` is required for the bridge to attach (-10005 IPC timeout);
#     `[Experts] Enabled=1` is the Algo Trading button — when off, every
#     order_send returns retcode 10027 "AutoTrading disabled by client" even
#     though attach/login work. MT5 self-updates and fresh installs reset both,
#     so enforce them BEFORE the terminal starts (it restores the button from
#     this file at launch). Idempotent: no-op when already 1.
CFG="${TERMINAL%/terminal64.exe}/Config/common.ini"
if [ -n "$TERMINAL" ] && [ -f "$CFG" ]; then
  python - "$CFG" <<'PY' || echo "[mt5] WARNING: could not patch $CFG"
import codecs, sys
p = sys.argv[1]
try:
    data = codecs.open(p, encoding="utf-16").read()
except Exception:
    sys.exit(1)
out = data.replace("Api=0", "Api=1").replace("Enabled=0", "Enabled=1")
if out != data:
    codecs.open(p, "w", encoding="utf-16").write(out)
    print("[mt5] patched [Experts] Api=1 Enabled=1 in common.ini")
PY
fi

# 6. Start the terminal (portable) so the bridge can attach.
if [ -n "$TERMINAL" ]; then
  echo "[mt5] Starting MT5 terminal: $TERMINAL"
  wine "$TERMINAL" /portable >/dev/null 2>&1 &
  sleep 20
  if pgrep -f "terminal64" >/dev/null 2>&1; then
    echo "[mt5] MT5 terminal is running."
  else
    echo "[mt5] WARNING: terminal64.exe is NOT running after launch. Check VNC on :5901. The bridge keeps retrying; a live Xvfb is required for the terminal to create its window."
  fi
else
  echo "[mt5] WARNING: no MT5 terminal found — check VNC / logs and finish the setup."
fi

# 7. Start the MT5 HTTP bridge (Wine Python + official MetaTrader5) — SUPERVISED.
#    The bridge is a one-shot child of this entrypoint; if it dies (Wine crash,
#    OOM kill, terminal regression) nothing restarts it and the bot loops with
#    "MT5 bridge unreachable ... Connection refused" forever. Wrap it in a
#    restart loop so it always comes back within a few seconds of dying.
if [ -f "$WPY_LINUX" ]; then
  echo "[mt5] Starting MT5 bridge (supervised — auto-restart on crash)..."
  (
    # The supervisor must never be killed by `set -e`: killing a process that
    # has already exited (e.g. `kill -9` after SIGTERM) returns non-zero and
    # would silently terminate the whole restart loop.
    set +e
    cd /app
    while :; do
      echo "[bridge] starting $(date -u +%FT%TZ)" >> /tmp/bridge.log
      wine "$WPY" mt5_bridge.py >> /tmp/bridge.log 2>&1 &
      BRIDGE_PID=$!
      # Health watchdog. Runs OUTSIDE Wine, so it still works when the bridge's
      # Python threads are stuck in a wedged mt5.initialize() (Wine can hold the
      # GIL there, so the bridge's own watchdog thread can't run). Only a bridge
      # that is truly UNRESPONSIVE is killed+restarted:
      #   - wget rc=0  -> /health returned 200 (terminal ready)          : healthy
      #   - wget rc=8  -> bridge answered with an HTTP error (e.g. 500,
      #                   terminal not logged in YET — can take 1-3 min) : alive
      #   - anything   -> no response at all (connection refused/timeout,
      #                   GIL-wedged initialize)                         : kill it
      # The per-check timeout (40s) is LONGER than the bridge's max request
      # time (~25s: 20s initialize cap + overhead), and the fail threshold is
      # generous (~5min) because the terminal can legitimately take 1-3 min to
      # authorize after a recreate. A slow-but-alive bridge is never mistaken
      # for a dead one; only a truly unresponsive bridge is killed+restarted.
      FAILS=0
      while kill -0 "$BRIDGE_PID" 2>/dev/null; do
        wget -q -O /dev/null --timeout=40 --tries=1 "http://127.0.0.1:18080/health" 2>/dev/null
        WRC=$?
        if [ "$WRC" -eq 0 ] || [ "$WRC" -eq 8 ]; then
          FAILS=0
        else
          FAILS=$((FAILS+1))
          if [ "$FAILS" -ge 6 ]; then
            echo "[bridge] watchdog: unresponsive to /health — killing (restarting)" >> /tmp/bridge.log
            kill "$BRIDGE_PID" 2>/dev/null
            for _ in $(seq 1 5); do
              kill -0 "$BRIDGE_PID" 2>/dev/null || break
              sleep 2
            done
            kill -9 "$BRIDGE_PID" 2>/dev/null
            break
          fi
        fi
        sleep 10
      done
      # Reap the child so kill -0 fails on the next iteration (no zombie loop).
      wait "$BRIDGE_PID" 2>/dev/null
      echo "[bridge] exited — restarting in 5s" >> /tmp/bridge.log
      sleep 5
    done
  ) &
  # Best-effort: wait (bounded) for /health so the bot's first cycle doesn't
  # immediately fail while the terminal authorizes. Never blocks forever.
  for i in $(seq 1 24); do
    if wget -q -O /dev/null --timeout=4 --tries=1 "http://127.0.0.1:18080/health" 2>/dev/null; then
      echo "[mt5] Bridge healthy (~$((i*5))s)."
      break
    fi
    sleep 5
  done
  tail -n 5 /tmp/bridge.log 2>/dev/null | sed 's/^/[bridge] /' || true
else
  echo "[mt5] WARNING: bridge not started (no Windows Python)."
fi

# 8. Run the bot (logs into the demo account and starts the MT5 strategy).
echo "[mt5] Starting bot..."
exec python -u bot.py
