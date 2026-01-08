# File Map

## Core Bot
| File | Purpose |
|------|---------|
| `mm_bot/runner_v5.py` | Main continuous market-making logic |
| `mm_bot/fill_tracker.py` | Fill tracking with txHash dedupe |
| `mm_bot/clob.py` | Polymarket CLOB wrapper |
| `mm_bot/config.py` | Configuration from environment |
| `mm_bot/market.py` | Market resolution |

## Scripts
| File | Purpose |
|------|---------|
| `scripts/mm_continuous.py` | Continuous bot runner |
| `scripts/mm_live_verify_once.py` | V12 single round-trip verifier |
| `scripts/check_trades_api_format.py` | Verify trades API fields |
| `scripts/check_status.py` | Quick account status check |

## Utilities
| File | Purpose |
|------|---------|
| `check_status.py` | Account balance/position check |
| `check_trades.py` | Trades API format check |

## Docs
| File | Purpose |
|------|---------|
| `docs/RUNBOOK.md` | Operations guide |
| `docs/STATE_MACHINE.md` | State transitions |
| `docs/FILEMAP.md` | This file |
| `docs/CHANGELOG.md` | Version history |
