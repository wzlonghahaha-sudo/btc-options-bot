# AGENTS.md — BTC Options Bot

Guidance for humans and coding agents working in this repository.

## Purpose

Monitor-only system for **BTC European options short-put** positions on Binance: risk alerts, opportunity scans, IV analytics, and Telegram commands. It is **information and alerting only** by default; live order placement is limited to opt-in emergency hedge flows documented in `.env.example`.

See `README.md` for architecture and module map.

## Directory layout

| Path | Role |
|------|------|
| `tg_bot_monitor.py` | Main loop: Telegram bot + scheduled scans |
| `binance_options.py` | Binance options API client |
| `risk_monitor.py`, `risk_rules.py`, `margin_calc.py`, `stress_test.py` | Risk engine and margin math |
| `opportunity_scanner.py`, `sell_put_strategy.py`, `profit_optimizer.py` | Scanning and scoring |
| `alert_channels.py`, `emergency_hedge.py` | Multi-channel CRITICAL alerts; optional auto-hedge |
| `proxy/` | Optional local sing-box failover for Binance egress |
| `tests/` | Offline pytest suite (`test_core.py`, `test_smoke.py`) |
| `scripts/test.sh` | One-command test runner |
| `.env.example` | Environment variable template (copy to `.env`, never commit) |

## Install

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` only when running the live bot locally; **tests do not require** API keys or Telegram tokens.

## Run tests (one command)

```bash
./scripts/test.sh
```

Equivalent:

```bash
python3 -m pytest tests/
```

All tests are offline: no network, no Telegram sends, no real orders.

## Run the bot (local)

```bash
./start_bot.sh
```

Requires a filled `.env` and outbound access to Binance/Telegram as configured.

## Code style

- Python 3, module-level docstrings for non-trivial files.
- Prefer pure functions in `margin_calc`, `risk_rules`, `iv_rank`, etc.; keep I/O in `tg_bot_monitor.py` and `binance_options.py`.
- Match existing naming (snake_case, bilingual comments where already present).
- Small, focused diffs; do not refactor unrelated modules in the same change.

## Hard rules

1. **Never commit** Telegram bot tokens, Binance API keys, SMTP passwords, proxy subscription URLs, or any `.env` file. Only `.env.example` belongs in git.
2. **Information only** for agent-driven changes: do not add features that place orders, enable trading, or turn on `EMERGENCY_AUTO_HEDGE` without explicit owner approval.
3. **Do not change alert thresholds**, stop-loss levels, margin formulas, or scoring weights that affect production alerts without owner approval.
4. **No live network calls in tests** — use mocks or pure functions; do not send Telegram messages in CI or agent runs.
5. Do not commit runtime artifacts (`bot.log`, `iv_history.json`, `charts/`, `data/`, `proxy/*.log`).

## Definition of done

- `./scripts/test.sh` passes (all offline tests green).
- No secrets or credentials in the diff.
- Runtime behavior unchanged unless the task explicitly requests a behavior change (and owner-approved for risk/alert parameters).
- `README.md` / `CHANGELOG.md` updated only when user-facing behavior or ops steps change.
