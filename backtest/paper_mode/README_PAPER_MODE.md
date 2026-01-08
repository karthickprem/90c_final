# Paper Mode - BTC 15m Polymarket Strategy

Paper trading runner for the BTC 15-minute Polymarket binary options strategy.

## Strategy Specification

**FINAL PRODUCTION CONFIG** (must match exactly):

### Entry

1. **Trigger**: First tick where `UP >= 90c` OR `DOWN >= 90c`
   - If both in same tick → TIE → skip window

2. **Validation Window** (10 seconds after trigger):
   - **SPIKE Filter** (chosen side only):
     - `min_side >= 88c`
     - `max_side >= 93c`
   - **JUMP Gate** (both sides):
     - `max(|delta|) < 8c` (no big jumps)
     - `count(|delta| >= 3c) < 2` (fewer than 2 mid-size jumps)

3. **Execution**:
   - Limit buy at `p_max = 93c`
   - Cancel if not filled within 2 seconds
   - Slippage model: `fill = min(p_max, tick_price + slip_entry)`

### Exit

- **TP**: Exit when `side >= 97c` (limit sell, no slippage)
- **SL**: Exit when `side <= 86c` (marketable sell, apply `slip_exit`)
- **Settlement**: If neither TP nor SL hit, settle at window end (100c or 0c)

### Sizing

- Default: `f = 2%` of bankroll per trade

## Usage

```bash
# Run paper mode (default 12 hours)
python -m backtest.paper_mode.runner --bankroll 100 --f 0.02

# Short test run
python -m backtest.paper_mode.runner --duration 0.5 --poll 1.0

# Custom parameters
python -m backtest.paper_mode.runner \
    --bankroll 1000 \
    --f 0.03 \
    --pmax 93 \
    --slip-entry 1 \
    --slip-exit 1 \
    --tp 97 \
    --sl 86 \
    --outdir out_paper_custom
```

### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--duration` | 12 | Hours to run |
| `--poll` | 1.0 | Poll interval in seconds |
| `--outdir` | out_paper | Output directory |
| `--bankroll` | 100 | Starting paper bankroll ($) |
| `--f` | 0.02 | Fraction of bankroll per trade |
| `--pmax` | 93 | Maximum entry price (cents) |
| `--slip-entry` | 1 | Entry slippage (cents) |
| `--slip-exit` | 1 | Exit slippage (cents) |
| `--tp` | 97 | Take profit threshold (cents) |
| `--sl` | 86 | Stop loss threshold (cents) |

## Output Files

### `out_paper/trades.jsonl`

One JSON object per line for each trade attempt:

```json
{
  "logged_at": "2026-01-05T23:30:00.000",
  "window_id": "btc-updown-15m-1736123400",
  "trigger_side": "UP",
  "trigger_price": 91,
  "trigger_ts": 1736123450.5,
  "spike_min": 89,
  "spike_max": 94,
  "jump_max_delta": 5,
  "jump_mid_count": 1,
  "entry_fill_price": 93,
  "entry_slip": 1,
  "shares": 21.5,
  "dollars_invested": 2.0,
  "exit_reason": "TP",
  "exit_price": 97,
  "exit_slip": 0,
  "pnl_invested": 0.043,
  "pnl_dollars": 0.086,
  "settle_winner": null,
  "ticks_count": 245
}
```

Skipped trades include `status: "SKIPPED"` with `skip_reason`.

### `out_paper/daily_summary.json`

Aggregate and per-day statistics:

```json
{
  "generated_at": "2026-01-05T23:45:00.000",
  "aggregate": {
    "trades": 15,
    "wins": 12,
    "losses": 3,
    "bankroll": 105.50,
    "starting_bankroll": 100.0,
    "pnl_total": 5.50,
    "pnl_pct": 5.5,
    "avg_entry": 91.3,
    "avg_exit": 95.8,
    "worst_loss": -0.0752,
    "max_drawdown": 0.015,
    "gap_count": 0,
    "severe_count": 0
  },
  "by_day": {
    "2026-01-05": {
      "trades_total": 20,
      "trades_executed": 15,
      "avg_entry": 91.3,
      "avg_exit": 95.8,
      "total_pnl": 5.5,
      "worst_trade": -0.0752,
      "gap_count": 0
    }
  }
}
```

## Architecture

```
backtest/paper_mode/
├── __init__.py
├── config.py          # Strategy + runtime configuration
├── strategy.py        # State machine (IDLE → OBSERVE → ENTRY → POSITION → DONE)
├── paper_broker.py    # Fill simulation + portfolio accounting
├── polymarket_client.py  # API adapter (window timing, prices)
├── logging_utils.py   # JSONL + summary writers
├── runner.py          # Main orchestrator + CLI
└── README_PAPER_MODE.md
```

### Module Responsibilities

- **config.py**: All thresholds (trigger, SPIKE, JUMP, TP/SL, sizing)
- **strategy.py**: Pure strategy logic, no API calls
- **paper_broker.py**: Position sizing, P&L calculation, portfolio stats
- **polymarket_client.py**: Only module that hits Polymarket APIs
- **logging_utils.py**: Trade logging, daily summaries
- **runner.py**: Polling loop, state machine orchestration

## Assertions / Safety

1. **No real orders**: This module never calls `client.post_order()` or any order-placing API
2. **Conservative fills**: Entry always applies `slip_entry` against you
3. **Conservative exits**: SL always applies `slip_exit` against you
4. **Limit cap enforced**: Entry never fills above `p_max`

## Interpreting Results

### Key Metrics

| Metric | Meaning |
|--------|---------|
| `pnl_invested` | (exit - entry) / entry per trade |
| `gap_count` | Trades with `pnl_invested <= -15%` |
| `severe_count` | Trades with `pnl_invested <= -25%` |
| `max_drawdown` | Largest peak-to-trough decline |

### Expected Performance (from backtest)

Based on historical backtesting with SPIKE + JUMP gates:

- Win rate: ~94%
- EV/invested: ~3-4% per trade
- Reversal rate: ~4-5%
- Gap events: ~0.4% of trades

### Warning Signs

If you see any of these, investigate before live trading:

1. **Avg entry > 94c**: Slippage model may be too optimistic
2. **Gap count > 1%**: Volatility regime change
3. **Win rate < 90%**: Strategy may not be triggering correctly
4. **Max drawdown > 10%**: Sizing or risk controls need review

## Replay Mode (Validation)

To validate paper mode matches backtest logic, use the tick replay fixture:

```python
# In tests/test_paper_mode.py
from backtest.paper_mode.strategy import StrategyStateMachine, Tick
from backtest.paper_mode.config import StrategyConfig

def test_replay_matches_backtest():
    # Load saved ticks from backtest
    ticks = load_tick_stream("fixtures/window_123.json")
    
    sm = StrategyStateMachine(StrategyConfig())
    sm.start_window("test", ticks[0].ts, ticks[-1].ts + 60)
    
    for tick in ticks:
        result = sm.process_tick(tick, bankroll=100)
    
    # Verify matches expected outcome
    assert sm.context.exit_reason == "TP"
    assert sm.context.realized_pnl_invested > 0
```


