# File Map - Polymarket MM Bot

> **Last Updated**: 2026-01-08
> **Current Version**: V6

## Canonical Files (USE THESE)

### Main Entry Points
| File | Purpose |
|------|---------|
| `scripts/mm_continuous.py` | **PRIMARY** - Run bot continuously across windows |
| `mm_bot/runner_v5.py` | **CORE** - V6 bot implementation (single window) |

### Core Modules
| File | Purpose |
|------|---------|
| `mm_bot/config.py` | Configuration, env parsing, defaults |
| `mm_bot/clob.py` | CLOB API wrapper (orders, books, balance) |
| `mm_bot/market.py` | Market discovery and token resolution |
| `mm_bot/positions.py` | Position tracking and MTM calculation |
| `mm_bot/fill_tracker.py` | Fill detection from REST, PnL tracking |

### Utility Scripts
| File | Purpose |
|------|---------|
| `scripts/mm_dryrun.py` | Test without trading |
| `scripts/mm_live_smoke.py` | Safe live test (post/cancel) |
| `scripts/mm_flatten_positions.py` | Manually flatten positions |
| `scripts/check_account.py` | Check account balance |

## Deprecated Files (DO NOT USE)

| File | Reason |
|------|--------|
| `mm_bot/runner.py` | Replaced by runner_v5.py |
| `mm_bot/runner_v2.py` | Replaced by runner_v5.py |
| `mm_bot/runner_v3.py` | Replaced by runner_v5.py |
| `mm_bot/runner_v4.py` | Replaced by runner_v5.py |
| `mm_bot/exit_supervisor.py` | Merged into runner_v5.py |
| `mm_bot/safety.py` | Merged into runner_v5.py |

## Version History

| Version | Key Changes |
|---------|-------------|
| V1 | Basic quoting |
| V2 | Added exit management |
| V3 | Endgame rules, stop-loss |
| V4 | Position reconciliation, regime filters |
| V5 | Opening mode, time-based exits |
| **V6** | Anti-pyramiding (15s cooldown), continuous mode |

## Directory Structure

```
mm_bot/
├── __init__.py
├── config.py          # Configuration
├── clob.py            # API wrapper
├── market.py          # Market discovery
├── positions.py       # Position tracking
├── fill_tracker.py    # Fill detection
├── runner_v5.py       # MAIN BOT (V6)
├── README_mm.md       # Documentation
└── tests/             # Unit tests

scripts/
├── mm_continuous.py   # MAIN RUNNER
├── mm_dryrun.py       # Test mode
├── mm_live_smoke.py   # Safe live test
└── mm_flatten_positions.py

docs/
├── FILEMAP.md         # This file
└── CHANGELOG.md       # Version history
```
