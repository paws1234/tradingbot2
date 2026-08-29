# Binance DeepSeek Trading Bot — Implementation Plan

## Project Overview

A fully **Dockerized Python** automated trading bot that:

1. Fetches the latest Binance klines (candlestick data)
2. Sends the last 20 candles to DeepSeek for a JSON trading decision
3. Enforces **hard risk guardrails** before placing any order
4. Places signed **MARKET orders** on the Binance **Demo Mode** exchange
5. Logs every decision and trade to persistent files

The bot targets a **$100 demo capital** account and is built for a **hands-free 7-day run**.

---

## Project Location

```
/home/workdir/artifacts/binance-deepseek-bot/
```

---

## File Structure

| File                  | Purpose                                                            |
| --------------------- | ------------------------------------------------------------------ |
| `bot.py`              | Full automated loop: fetch klines → DeepSeek JSON decision → risk guardrails → Demo market order → logging |
| `Dockerfile`          | Python 3.12-slim image                                             |
| `docker-compose.yml`  | One-command run with persistent logs                               |
| `.env.example`        | All config keys (template)                                         |
| `requirements.txt`    | `requests`, `openai` (DeepSeek-compatible), `python-dotenv`, `schedule`, `tenacity` |
| `README.md`           | Setup & usage instructions                                         |

---

## Phase 1 — Setup

- Use Binance **Demo Mode** base URL: `https://demo-api.binance.com`
- Credentials are provided via `.env` — **never hard-coded**
- Copy `.env.example` → `.env` and fill in:
  - Binance Demo API Key + Secret
  - DeepSeek API Key

### Quick Start

```bash
cd /home/workdir/artifacts/binance-deepseek-bot

cp .env.example .env
# Edit .env → paste your Binance Demo API Key/Secret + DeepSeek API key

docker compose up -d --build
docker compose logs -f bot
```

---

## Phase 2 — Automated Loop

The core loop inside `bot.py` runs in this order:

1. **Fetch klines** — pull latest candles from `/api/v3/klines`
2. **Send to DeepSeek** — pass the last 20 candles with a strict JSON-schema prompt
3. **Parse JSON** — extract only clean fields:
   - `action`
   - `quantity`
   - `stop_loss`
   - `take_profit`
   - `confidence`
   - `reason`
4. **Risk guardrails** (the critical safety layer) — **reject** any trade that fails:
   - ❌ No `stop_loss` → reject
   - ❌ Max loss > 2% of the $100 target → reject
   - ❌ Position > $50 notional → reject
   - ❌ Notional < $10 minimum → reject
   - ❌ Confidence < 0.55 threshold → reject
5. **Place order** — send a signed MARKET order to Demo
6. **Log** — append structured records to `logs/trades.jsonl` + a human-readable log

---

## Phase 3 — Hands-free 7-Day Run

- Runs once **immediately** on startup, then every `RUN_EVERY_HOURS` (default `1`)
- `restart: unless-stopped` → survives container/daemon restarts
- Logs persist via a **volume mount** (no data loss on restart)

---

## Risk Parameters (Defaults for ~$100)

| Parameter                | Default | Description                          |
| ------------------------ | ------- | ------------------------------------ |
| `TARGET_CAPITAL_USD`     | `100`   | Target demo account capital          |
| `MAX_RISK_PCT`           | `0.02`  | Max loss per trade = 2% of capital   |
| `MAX_POSITION_USD`       | `50`    | Hard cap on position notional        |
| `MIN_NOTIONAL_USD`       | `10`    | Minimum trade size                   |

All parameters are configurable via `.env` **without rebuilding the image**.

---

## Implementation Steps

- [ ] Create project directory & scaffold all files
- [ ] Implement `bot.py` (fetch → decide → guardrail → order → log)
- [ ] Write `Dockerfile` (Python 3.12-slim)
- [ ] Write `docker-compose.yml` (persistent logs, `restart: unless-stopped`)
- [ ] Write `.env.example` with all config keys
- [ ] Write `requirements.txt` (with `tenacity` for retry resilience)
- [ ] Write `README.md` with setup & usage docs
- [ ] Test on Binance **Demo Mode** only

---

## Important Notes & Next Steps

- ⚠️ **Demo Mode only** — the same code pointed at live API keys would be dangerous.
- The bot places plain **MARKET orders** and immediately follows up with an **OCO order** (stop-loss + take-profit) so no position is ever left unprotected.
- It manages **one protected position at a time** — new entries are skipped while a position is open.
- DeepSeek model defaults to `deepseek-chat` (OpenAI-compatible endpoint). Switchable via `DEEPSEEK_MODEL`.

---

## Safety Principles

1. Never hard-code credentials — always use `.env`
2. Always enforce stop-loss — reject any trade without one
3. Cap position size and risk per trade
4. Run against Demo before ever considering live trading
