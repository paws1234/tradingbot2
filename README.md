# Binance DeepSeek Trading Bot (Demo Mode)

A fully Dockerized Python bot that automates a market-analysis → decision → trade workflow on the **Binance Demo exchange**:

1. Fetches the latest klines (`/api/v3/klines`)
2. Computes **quantitative indicators** (SMA/EMA/RSI/MACD/Bollinger/ATR/volume) in code and sends them — plus the last 20 candles — to DeepSeek with a strict JSON-schema prompt
3. Parses only clean JSON (`action`, `quantity`, `stop_loss`, `take_profit`, `confidence`, `reason`)
4. Enforces **hard risk guardrails** before placing any order
5. Manages positions — **one protected position at a time**, never stacked
6. Places a signed **MARKET order** on Demo
7. Immediately places a **stop-loss + take-profit (OCO)** so no position is ever left unprotected
8. Logs every decision and trade to `logs/trades.jsonl` + `logs/bot.log`
9. **Telegram control**: switch between `swing` (1h), `scalping` (3m) and `mt5` (MetaTrader 5, XAUUSD+EURUSD) setups on the fly, pause/resume, and get every cycle result pushed to your chat

> ⚠️ **DEMO MODE ONLY.** The same code pointed at live API keys would be dangerous. Never use live keys.

---

## Quick Start

```bash
cp .env.example .env
# Edit .env → paste your Binance Demo API Key/Secret + DeepSeek API key.
# Optional: add TELEGRAM_BOT_TOKEN (from @BotFather) for remote control.

docker compose up -d --build
docker compose logs -f bot
```

The bot runs **once immediately** on startup, then repeats every `RUN_EVERY_HOURS` (default 1 hour).

---

## Configuration (`.env`)

| Variable                | Default                   | Description                                   |
| ----------------------- | ------------------------- | --------------------------------------------- |
| `BINANCE_DEMO_URL`      | `https://demo-api.binance.com` | Binance Demo base URL                    |
| `BINANCE_DEMO_API_KEY`  | —                         | Your Binance Demo API key                     |
| `BINANCE_DEMO_API_SECRET` | —                       | Your Binance Demo API secret                  |
| `DEEPSEEK_API_KEY`      | —                         | Your DeepSeek API key                         |
| `DEEPSEEK_MODEL`        | `deepseek-v4-flash`       | DeepSeek model to use                         |
| `DEEPSEEK_THINKING`     | `true`                    | Enable thinking mode for reasoned decisions   |
| `DEEPSEEK_MAX_TOKENS`   | `10000`                   | Max output tokens (incl. reasoning)           |
| `DEEPSEEK_REASONING_EFFORT` | `high`                | Reasoning effort when thinking is enabled     |
| `SYMBOL`                | `BTCUSDT`                 | Trading pair                                  |
| `INTERVAL`              | `1h`                      | Candle interval                               |
| `CANDLES`               | `20`                      | Number of candles shown to DeepSeek           |
| `FETCH_LIMIT`           | `120`                     | History fetched to compute quant indicators (SMA/EMA/MACD/BB/ATR/RSI) |
| `TELEGRAM_BOT_TOKEN`    | —                         | Telegram bot token from @BotFather (enables remote control) |
| `TELEGRAM_CHAT_ID`      | —                         | Optional: lock control to one chat id (auto-captured on first /start if empty) |
| `DEFAULT_STRATEGY`      | `swing`                   | Initial strategy: `swing` (1h) or `scalping` (3m) |
| `TARGET_CAPITAL_USD`    | `100`                     | Target demo account capital                   |
| `MAX_RISK_PCT`          | `0.02`                    | Max loss per trade (2% of capital)            |
| `MAX_POSITION_USD`      | `50`                      | Hard cap on position notional                 |
| `MIN_NOTIONAL_USD`      | `10`                      | Minimum trade size                            |
| `CONFIDENCE_THRESHOLD`  | `0.55`                    | Minimum model confidence to trade             |
| `RUN_EVERY_HOURS`       | `1`                       | Loop interval in hours                        |

All parameters can be changed in `.env` **without rebuilding the image**.

---

## Risk Guardrails

A trade is **rejected** (and logged) if any of these fail:

- ❌ No `stop_loss` provided
- ❌ Model confidence below `CONFIDENCE_THRESHOLD`
- ❌ Notional below `MIN_NOTIONAL_USD`
- ❌ Notional above `MAX_POSITION_USD`
- ❌ Projected loss (`(entry − stop_loss) × qty`) above `MAX_RISK_PCT × TARGET_CAPITAL_USD`
- ❌ `stop_loss` on the wrong side of the current price
- ❌ `take_profit` on the wrong side of the current price

Additionally, the bot opens **one position at a time**: if a position (or its protective orders) is already open, new entries are skipped. After every entry fill, a **stop-loss (and take-profit when provided)** is placed immediately on the exchange, so positions are never left unprotected.

## LLM Position Manager (all strategies)

Since v1, DeepSeek only made **entry** decisions. With the **position manager** (default **on** for `swing`, `scalping` and `mt5`), DeepSeek now also **manages every open position**: each cycle, when a position is open, the LLM is fed the position (side, qty/lots, entry, current price, unrealized PnL, SL/TP) plus the live indicators and decides one of:

- **HOLD** — keep the position as-is
- **TRAIL** — move the stop-loss to breakeven / behind price to lock in profit
- **EXIT** — market-close now (trend flip, target hit, bad momentum)

Hard guardrails protect the account:
- A trailing stop **can never widen** or be placed **past the current price** (LONG: `old_sl ≤ new_sl < price`; SHORT: `price < new_sl ≤ old_sl`) — any invalid proposal is rejected and logged.
- **EXIT** cancels protective orders then market-closes; **TRAIL** replaces the protection atomically (Binance: cancel OCO → re-place with the new SL; MT5: native `TRADE_ACTION_SLTP` modify).
- If a trail's new protection fails to place, the bot **safety-closes** — never left naked.

Every management decision is logged to `logs/trades.jsonl` (`manage_hold` / `manage_trail` / `manage_exit`) and posted to Telegram. The Binance entry price is tracked in `logs/state.json` (the demo API doesn't expose it); MT5 positions are read live from the terminal with real PnL. Set `POSITION_MANAGER=false` in `.env` to revert to the old skip-if-open behaviour.

---

## Telegram Control (A/B test strategies)

The bot runs **`swing`** (the original 1h setup) by default. Once you add `TELEGRAM_BOT_TOKEN`, message your bot `/start` to register, then:

| Command | Action |
| --- | --- |
| `/status` | Current strategy, price, balance, open orders |
| `/strategy swing` | Switch to the 1h swing setup |
| `/strategy scalping` | Switch to the 3m momentum scalping setup |
| `/strategy mt5` | Switch to the MetaTrader 5 setup (XAUUSD+EURUSD, 15m trend + 5m entry) |
| `/pause` / `/resume` | Pause / resume trading cycles |
| `/help` | List commands |

The chosen strategy **persists across restarts** (`logs/state.json`), switches take effect immediately (one cycle runs right away), and every cycle result is posted to your chat so you can compare which strategy is profitable. Scalping uses a **fast LLM mode** (thinking disabled, ~2k tokens) for sub-5s decisions and a tighter 0.5×ATR stop / 0.20–0.35% take-profit.

Each strategy has its own `.env` overrides: `SWING_INTERVAL`, `SWING_CANDLES`, `SWING_FETCH_LIMIT`, `SWING_RUN_EVERY_MINUTES`, `SCALP_INTERVAL`, `SCALP_CANDLES`, `SCALP_FETCH_LIMIT`, `SCALP_RUN_EVERY_MINUTES`.

> Only the admin chat can control the bot. Set `TELEGRAM_CHAT_ID` to pre-lock it, or the first user to send `/start` becomes admin.

---

## MetaTrader 5 (MT5) Setup — XAUUSD + EURUSD

A third, **fully separate** setup: `mt5` trades **Gold (XAUUSD)** and **EURUSD** on a **MetaTrader 5 DEMO** account using a **M15 trend filter + M5 entry** confluence. It switches exclusively via `/strategy mt5` — the Binance `swing`/`scalping` setups are untouched.

> ⚠️ MT5 is **forex/CFD**: both BUY and SELL (shorts) are allowed, sizing is in **lots** (not base units), and SL/TP are attached atomically to the entry order (MT5 has no OCO).

### How it connects (Linux / Wine bridge)

The `MetaTrader5` pip package is **Windows-only** — there is no official Linux build. On Linux the bot reaches the MT5 terminal through a **Wine bridge**: the MT5 terminal runs under Wine, and a small process (`mt5_bridge.py`) runs under **Windows Python in Wine** using the official `MetaTrader5` package, exposing a tiny HTTP API. The bot (`mt5_venue.py`) calls that HTTP API — no third-party bridge binaries.

**Layout B (everything in Docker):**
1. `docker compose -f docker-compose.mt5.yml up -d --build` — the image ships Wine + Xvfb + VNC. On first start the entrypoint installs Windows Python + `MetaTrader5` into Wine and the MT5 terminal (a few minutes; watch `docker compose -f docker-compose.mt5.yml logs -f`).
2. If the auto-login (via `MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER`) doesn't complete, open the terminal over **VNC at `localhost:5901`** (no password) and log into your demo account once — that login is persisted in the `mt5_wine` volume.
3. Message the bot `/strategy mt5`.

> ⚠️ **Algo Trading must be enabled** for the bot to place orders — otherwise MT5 rejects them with `retcode 10027 'AutoTrading disabled by client'`. The working setup has it enabled in the terminal config (`Config/common.ini` → `[Experts] Enabled=1`, persisted in the `mt5_wine` volume). If a fresh setup has it off, click the **Algo Trading** button in the terminal toolbar (or Tools → Options → Expert Advisors) once.

> Switching back to Binance-only is just `docker compose up -d --build` — the `mt5` strategy stays defined but simply won't run without the Wine bridge (it logs a clear error instead of crashing).

### MT5 configuration (`.env`)

| Variable | Default | Description |
| --- | --- | --- |
| `MT5_LOGIN` | — | MT5 demo account login |
| `MT5_PASSWORD` | — | MT5 demo account password |
| `MT5_SERVER` | — | Broker server name (e.g. `MetaQuotes-Demo`) |
| `MT5_SYMBOLS` | `XAUUSD,EURUSD` | Symbols traded (exact Market Watch names) |
| `MT5_TREND_INTERVAL` | `m15` | Higher-timeframe trend filter |
| `MT5_ENTRY_INTERVAL` | `m5` | Lower-timeframe entry |
| `MT5_RUN_EVERY_MINUTES` | `5` | Cycle cadence |
| `MT5_MIN_LOT` / `MT5_MAX_LOT` | `0.01` / `1.0` | Lot-size bounds |
| `MT5_RISK_PCT` | `0.02` | Max loss per trade as % of MT5 balance |
| `MT5_FAST_LLM` | `true` | Fast (no-thinking) DeepSeek mode |
| `MT5_VOLUME_MIN` | `1.0` | **Hard** min volume ratio for an entry (enforced in code, not just the prompt) |
| `MT5_RSI_MIN` / `MT5_RSI_MAX` | `25` / `75` | **Hard** no-chase RSI(7) band — rejects overbought/oversold entries |
| `MT5_TRAIL_MIN_ATR` | `0.5` | Min trailing-stop distance = this × ATR(entry) (stops can't be tightened inside noise) |
| `MT5_MIN_STOP_ATR` | `0.5` | **Hard** min entry stop distance = this × ATR(entry) — rejects noise-tight stops at open |
| `MT5_FORCE_EXIT_FLIP` | `true` | Safety-exit when the M15 trend clearly flips against the position |
| `MT5_FORCE_EXIT_LOSS_SL_MULT` | `0.5` | Safety-exit when loss ≥ this × entry-to-SL distance (cuts losses before the broker SL) |
| `MT5_BRIDGE_URL` | `http://localhost:18080` | The in-container Wine-Python MT5 HTTP bridge |

All MT5-specific logic is isolated in **`mt5_venue.py`**; `bot.py` only routes the `mt5` strategy through it. The MT5 cycle fetches both timeframes, asks DeepSeek for a lot-sized decision (SELL allowed), enforces its own risk guardrails, and places the market order with **atomic SL+TP** so a position is never unprotected.

---

## Logs

- **`logs/bot.log`** — human-readable, timestamped cycle log
- **`logs/trades.jsonl`** — structured records: one JSON object per cycle (`filled` / `rejected` / `error`)

Logs persist across container restarts via the `./logs` volume mount. The container is set to `restart: unless-stopped`.

---

## Next Steps

- Review `logs/trades.jsonl` daily to evaluate DeepSeek decision quality.
- The bot already places protective **stop-loss + take-profit** after every entry. Tune the risk parameters in `.env` based on observed performance.

---

## Run Locally (without Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```
