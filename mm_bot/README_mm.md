# Polymarket BTC 15-min Market Making Bot (V6)

## Overview

A production-grade market-making bot for Polymarket's BTC 15-minute Up/Down markets. The bot earns maker rebates by providing liquidity while managing risk through strict position limits and automated exits.

## Strategy

### Core Concept
- **Maker Rebate Harvesting**: Post limit orders (postOnly) to earn daily USDC rebates
- **Single Position Only**: Never hold more than one position at a time (anti-pyramiding)
- **Time-Based Exits**: Exit positions within 20-40 seconds using TP/scratch/flatten ladder
- **Regime Filtering**: Only trade when market is balanced (30-70% range, low volatility)

### How It Works
1. **Entry**: Post a BUY limit order at best_bid when regime filters pass
2. **Fill Detection**: Detect fills via REST reconciliation (every 0.5-2s)
3. **Exit**: Immediately post SELL at entry+2c (TP), reprice to scratch after 20s
4. **Flatten**: Cross spread as taker after 40s if emergency enabled

### Risk Controls
- **15s Entry Cooldown**: Prevents multiple orders before fill confirmation
- **Inventory Gate**: No new entries if any position exists
- **Regime Filters**: Only trade in 0.30-0.70 mid range, volatility < 12c
- **Endgame Rules**: No entries in last 3 min, flatten in last 2 min
- **Dynamic Sizing**: Position size = 15% of account balance

## Quick Start

### Run Continuous Bot (Recommended)
```powershell
# PowerShell
cd C:\Users\karthick\Documents\tmp
$env:LIVE="1"
$env:MM_EXIT_ENFORCED="1"
$env:MM_EMERGENCY_TAKER_EXIT="1"
python -u scripts/mm_continuous.py
```

```cmd
# CMD
cd C:\Users\karthick\Documents\tmp
set LIVE=1
set MM_EXIT_ENFORCED=1
set MM_EMERGENCY_TAKER_EXIT=1
python -u scripts/mm_continuous.py
```

### Run Single Window
```powershell
$env:LIVE="1"; $env:MM_EXIT_ENFORCED="1"; $env:MM_EMERGENCY_TAKER_EXIT="1"
python -u -m mm_bot.runner_v5 --seconds 900 --outdir mm_out
```

## Configuration

### Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `LIVE` | `0` | Enable live trading (1=live, 0=dryrun) |
| `MM_EXIT_ENFORCED` | `0` | Required for live mode |
| `MM_EMERGENCY_TAKER_EXIT` | `0` | Allow crossing spread to flatten |
| `MM_MAX_USDC_LOCKED` | `2.50` | Max USDC per position |
| `MM_QUOTE_SIZE` | `5` | Shares per order (min 5) |

### Key Parameters (in runner_v5.py)
```python
# Position Sizing
MIN_SHARES = 5.0          # Minimum order size
SHARE_STEP = 0.01         # Rounding step

# Regime Filters
ENTRY_MID_MIN = 0.30      # Only enter if mid > 30%
ENTRY_MID_MAX = 0.70      # Only enter if mid < 70%
VOL_10S_CENTS = 12.0      # Max 5s volatility (cents)
MIN_SPREAD_CENTS = 1.0    # Min spread to quote

# Exit Timing
EXIT_TP_CENTS = 2.0       # Take profit: entry + 2c
EXIT_SCRATCH_SECS = 20.0  # Reprice to scratch after 20s
EXIT_FLATTEN_SECS = 40.0  # Emergency flatten after 40s

# Endgame
ENTRY_CUTOFF_SECS = 180   # No entries in last 3 min
FLATTEN_DEADLINE_SECS = 120  # Flatten in last 2 min

# Anti-Pyramiding
ENTRY_COOLDOWN_SECS = 15.0  # Wait 15s between entries
```

## File Structure

### Core Modules (`mm_bot/`)
| File | Description |
|------|-------------|
| `runner_v5.py` | **MAIN** - V6 bot with all fixes |
| `config.py` | Configuration and env parsing |
| `clob.py` | CLOB API wrapper (orders, books) |
| `market.py` | Market discovery and resolution |
| `positions.py` | Position tracking and MTM |
| `fill_tracker.py` | Fill detection and PnL |

### Scripts (`scripts/`)
| Script | Description |
|--------|-------------|
| `mm_continuous.py` | **MAIN** - Continuous multi-window runner |
| `mm_dryrun.py` | Test without trading |
| `mm_live_smoke.py` | Safe live test (post/cancel) |
| `mm_flatten_positions.py` | Manually flatten positions |

## V6 Fixes (Latest)

### P0: Anti-Pyramiding
- **15s Entry Cooldown**: After posting an order, wait 15s before posting another
- **Track All Orders**: Cancel all entries when inventory appears
- **Inventory Gate**: No entries if any position > 0

### P1: Exit Reliability  
- **Cancel-Confirm-Post**: Cancel existing exit before posting new
- **avgPrice Fallback**: Use book price if API returns 0
- **Refresh on Balance Error**: Re-fetch positions and clamp size

### P2: Continuous Operation
- **Auto Window Transition**: Detects new market windows
- **Dynamic Sizing**: Adjusts position size to 15% of balance
- **Session Summaries**: Reports PnL per window

## Expected Performance

Based on testing:
- **Win Rate**: ~60-70% (depends on market conditions)
- **Avg Hold Time**: 10-30 seconds
- **Target PnL**: Breakeven on trades + maker rebates
- **Rebates**: Paid daily at UTC 0:00

## Safety Notes

1. **Start Small**: Use $2.50 max position on $15-20 account
2. **Monitor First Sessions**: Watch for unexpected behavior
3. **Check Positions**: If bot crashes, manually check Polymarket UI
4. **Rebates Are Bonus**: Don't rely on rebates for profitability
