# Polymarket BTC 15-Min Arbitrage - Final Report

## What Changed: V1 → V2 → V3 → V4

| Version | Problem | Fix |
|---------|---------|-----|
| **V1** | Bought 250 shares one-sided, no hedge | Added hedge feasibility gate |
| **V2** | Posted bids but no completion logic | Added adaptive completion |
| **V3** | No queue realism, fixed time runs | Added Model M (persistent fills) |
| **V4** | No queue penalty, no pre-filter | Added Model Q, quote cadence, 50/50 filter |

## V4 Mechanics (Current)

### Three Fill Models
| Model | Rule | Purpose |
|-------|------|---------|
| L (Optimistic) | `ask <= bid` instantly | Upper bound |
| M (Mid) | `ask <= bid` for 3+ ticks | Realistic mid |
| Q (Pessimistic) | 5+ ticks + 30% book depletion + 50% partial fill | Lower bound |

### Hard Invariants
```
completion_bid <= 1 - edge_floor - first_leg_price   # Never violated
If max_completion <= best_bid_other + tick → rescue mode or stop
```

### Quote Cadence
- Min lifetime: 2 seconds
- Max cancels per window: 20
- Tracked: cancels, replaces, avg_quote_lifetime

### Pre-filter
Only trade windows where: `abs(mid_up - 0.5) + abs(mid_down - 0.5) <= 0.10`

## Metrics That Decide

### Primary
- **P(complete | first fill)** by model L/M/Q

### Edge Distribution
- `edge_net = 1 - (fill_price_leg1 + fill_price_leg2)`
- Report: **median** and **10th percentile**

### Risk
- `time_unhedged` distribution
- `max_unhedged_exposure` distribution

### Stratification
- By `volatility_proxy` (high >2% vs low ≤2%)
- By `price_distance` bucket

## Decision Framework

```
IF first_leg_fills_Q < 100:
    → INCONCLUSIVE: need more data

IF first_leg_fills_Q >= 100:
    IF P(complete|first)_Q < 20%:
        → KILL: market too trend-dominated
    
    ELIF P(complete|first)_Q > 50% AND median_edge_Q > 0.5¢:
        → PROMISING: consider live test
    
    ELSE:
        → MARGINAL: edge too thin
```

## How to Run

```bash
# Target 200 first-leg fills (min 100 for decision)
python -m pm_15m_arb.engine_v4 --target-fills 200 --min-fills 100

# With custom parameters
python -m pm_15m_arb.engine_v4 \
    --target-fills 200 \
    --min-fills 100 \
    --edge-floor 0.005 \
    --prefilter 0.10 \
    --max-hours 24
```

## Output Files

```
pm_results_v4/
├── trades_v4_YYYYMMDD_HHMMSS.jsonl    # All events
└── results_v4_YYYYMMDD_HHMMSS.json    # Summary with L/M/Q band
```

## Final Verdict

| Strategy | Status |
|----------|--------|
| **Variant A (Taker arb)** | DEAD. ask_sum ≥ 1.01 always. |
| **V1 (Unconstrained)** | INVALID. Not arbitrage. |
| **V4 (Maker pair-capture)** | VIABLE ONLY IF: survives Model Q with P(complete\|first) > 50% in near-50/50 + high-vol windows. |

---

*Run until 100+ first-leg fills under Model Q. If P(complete|first) < 20%, kill with confidence.*

