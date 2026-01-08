# BTC 15-Minute Market: Complete Algorithm Analysis

## Executive Summary

After exhaustive backtesting with 50 days of data (4,867 windows), we found:

| Strategy | Profitable? | Net PnL (51 days) | Monthly |
|----------|-------------|-------------------|---------|
| **Full-set (≤96c)** | ✅ YES | **$145** | **$85** |
| **Late 99c (110s window)** | ✅ YES | **$10** | **$6** |
| Directional at 90c | ❌ NO | -$250 | -$147 |
| Reversal-filtered entries | ❌ NO | -$70 | -$41 |
| Fade the spike | ❌ NO | -$460 | -$271 |
| Momentum confirmation | ❌ NO | N/A | N/A |
| Contrarian (spike failure) | ❌ NO | -$11 | -$6 |
| Double reversal | ❌ NO | -$254 | -$149 |

## Why Most Strategies Fail

### The Math Problem

At 90c entry:
- Win profit: 10c per share
- Loss: 90c per share
- **Required win rate: ~90%**
- **Actual win rate: ~88%**
- **Result: LOSS**

At 99c entry:
- Win profit: 1c per share
- Loss: 99c per share
- **Required win rate: ~99%**
- **Actual win rate: ~98.5%**
- **Result: LOSS (barely)**

### The Fee Problem

Polymarket taker fees:
```
Fee = shares × 0.25 × (price × (1-price))²
```

At 50c: ~1.5% of notional
At 90c: ~0.2% of notional
At 99c: ~0.002% of notional

Even small fees destroy marginal edges.

## What DOES Work

### 1. Full-Set Arbitrage (Best Strategy)

**When:** Combined cost (UP_ask + DOWN_ask) ≤ 96c
**Edge:** 4c+ per pair
**Win rate:** 100% (guaranteed)
**Frequency:** ~3-4 per day
**Net profit:** ~$145 / 51 days

**How it works:**
```
Buy UP at 48c + Buy DOWN at 48c = 96c total
Settlement: One side pays 100c
Profit: 100c - 96c = 4c (minus ~0.6c fees = 3.4c net)
```

### 2. Perfect Timing at 99c

**When:** Price = 99c AND time remaining = 105-115 seconds
**Win rate:** 100%
**Frequency:** ~2 per day
**Net profit:** ~$10 / 51 days

**Why it works:**
At exactly this time window, 99c prices that exist are almost certain to settle at 100c.

## Combined Profitable Strategy

```
Total: ~$155 / 51 days = ~$91/month = ~$1,100/year

With $1,000 capital (100x): ~$9,100/month
With $10,000 capital (1000x): ~$91,000/month (theoretical - liquidity limits)
```

## Why @0x8dxd Makes More Money

The wallet we analyzed (25,807 full-set pairs) uses:

1. **MAKER orders** - Earns rebates instead of paying fees
2. **High frequency** - Trades constantly
3. **Low latency** - Captures opportunities before others
4. **All markets** - Not just BTC 15m

### Maker vs Taker Economics

| Trade | Taker | Maker |
|-------|-------|-------|
| Fee at 97c | -$0.04 | **+$0.05 rebate** |
| Break-even WR | 97% | **~94%** |
| 97c viable? | NO | **YES** |

**If you can consistently get maker fills, even 97-99c entries become profitable.**

## Realistic Expectations

### Can a Retail Trader Profit?

| Approach | Realistic? |
|----------|------------|
| Full-set only | ✅ Yes, but small (~$90/month at $10/trade) |
| Scale to $1000/trade | ⚠️ Maybe, liquidity limits exist |
| Compete with bots | ❌ No, they're faster |
| Maker orders | ⚠️ Requires infrastructure |

### What You'd Need to Match @0x8dxd

1. Low-latency infrastructure
2. Maker order management
3. 24/7 automated trading
4. Multi-market deployment
5. Capital for scale

## Conclusion

**Q: Is there a profitable algorithm in BTC 15m?**

**A: YES, but limited:**
- Full-set arbitrage: ~$90/month at base size
- Late 99c entries: ~$6/month bonus
- Total: ~$96/month at $10/trade

**Q: Can you get rich?**

**A: Not easily:**
- Edge is small (~3-4c per full-set)
- Frequency is low (~4/day)
- Scaling is limited by liquidity
- You're competing against bots

**Q: What's the real opportunity?**

**A: Be a MAKER, not a TAKER:**
- Post orders instead of taking
- Earn rebates instead of paying fees
- This flips the economics entirely
- But requires infrastructure investment

## Files Created

| File | Purpose |
|------|---------|
| `reversal_detector.py` | Analyzes spike reversal patterns |
| `fee_analysis.py` | Polymarket fee curve analysis |
| `opportunity_count.py` | Full-set opportunity frequency |
| `alternative_strategies.py` | Tests 5 different strategies |
| `late_99c_strategy.py` | Perfect timing analysis |
| `fullset_only_strategy.py` | Final profitable strategy |
| `perfect_timing_strategy.py` | Combined optimal approach |

