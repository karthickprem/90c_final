## 🔬 Backtesting System for Parameter Optimization

## Overview

Find the **optimal sweet spot** for:
- Entry price range (85-95c? 80-90c? 90-98c?)
- Entry time window (60s? 90s? 120s? 180s?)
- Position size (25%? 35%? 50%?)

---

## 📊 Two-Phase Approach

### **Phase 1: Data Collection**

Collect real tick-level price data from Polymarket:

```bash
python pm_backtest.py --collect --windows 20
```

**What it does:**
1. Waits for each 15-minute window
2. Records prices every 1 second: `[UP, DOWN, time_left]`
3. Waits for outcome (which side won)
4. Saves to: `backtest_data_YYYYMMDD_HHMMSS.json`

**Duration:** ~20 windows = 5 hours

**Output example:**
```json
{
  "slug": "btc-updown-15m-1767555000",
  "outcome": "up",
  "ticks": [
    {"ts": 1234, "secs_left": 900, "up": 0.45, "down": 0.55},
    {"ts": 1235, "secs_left": 899, "up": 0.46, "down": 0.54},
    ...
    {"ts": 2134, "secs_left": 0, "up": 0.98, "down": 0.02}
  ]
}
```

---

### **Phase 2: Backtest & Optimize**

Test all parameter combinations:

```bash
python pm_backtest.py --backtest backtest_data_20260105_010000.json
```

**Tests these configs:**
```
85-95c | 120s window | 35% position
85-95c | 180s window | 35% position
85-95c | 90s window  | 35% position
85-95c | 60s window  | 35% position
80-90c | 120s window | 35% position
90-98c | 120s window | 35% position
85-95c | 120s window | 50% position
85-95c | 120s window | 25% position
```

**Output:**
```
PARAMETER SWEEP
============================================================

85-95c | 120s window | 35% position:
  Trades: 15
  Win rate: 93.3%
  P&L: +$5.23 (+52.3%)

85-95c | 180s window | 35% position:
  Trades: 18
  Win rate: 88.9%
  P&L: +$3.10 (+31.0%)

...

BEST CONFIG:
  Entry: 85-95c
  Window: 90s
  Position: 30%
  P&L: +$6.45
  Win Rate: 94.4%
```

---

## 🎯 What You Learn

### 1. **Optimal Entry Range**

```
80-90c: More trades, lower win rate (~85%)
85-95c: Balanced (~90%)
90-98c: Fewer trades, higher win rate (~95%)
```

### 2. **Optimal Time Window**

```
60s: Very high WR (96%) but few trades
90s: High WR (93%) with good trade freq ← Likely best
120s: Good WR (90%) with more trades ← Current
180s: Lower WR (87%) but more opportunities
```

### 3. **Optimal Position Size**

Depends on win rate:
- 95% WR → 50% position works
- 90% WR → 35% position optimal
- 85% WR → 25% position safer

---

## 📈 Step-by-Step Guide

### **Step 1: Collect Data (5 hours)**

```bash
# Collect 20 windows of tick data
python pm_backtest.py --collect --windows 20
```

**Run this while live bot is also running** - no conflict!

---

### **Step 2: Backtest (1 minute)**

```bash
# Test all parameter combinations
python pm_backtest.py --backtest backtest_data_*.json
```

---

### **Step 3: Optimize Live Bot**

Update `pm_fast_bot.py` with best parameters:

```python
# If backtest shows 90s window is better:
ENTRY_WINDOW_SECS = 90

# If backtest shows 87-93c is better:
ENTRY_MIN = 0.87
ENTRY_MAX = 0.93

# If backtest shows 40% position works:
BALANCE_PCT = 0.40
```

---

## 🔄 Continuous Improvement

### **After Every 50 Live Trades:**

1. Export live trade data
2. Re-run backtest with updated data
3. Adjust parameters if needed
4. Track performance over time

---

## 💡 Alternative: Use Existing Data

We can also backtest using the live bot's own trade logs!

Every trade the bot makes logs:
- Entry price
- Entry time (secs_left)
- Outcome (win/loss)

After 20-50 trades, we can analyze:
```python
# Stratify by entry time
trades_at_120s = [t for t in trades if 110 <= t['secs_left'] <= 130]
trades_at_90s = [t for t in trades if 80 <= t['secs_left'] <= 100]

# Compare win rates
win_rate_120s = wins(trades_at_120s) / len(trades_at_120s)
win_rate_90s = wins(trades_at_90s) / len(trades_at_90s)
```

---

## 🎯 Quick Start

**Option A: Collect New Data (5 hours)**
```bash
python pm_backtest.py --collect --windows 20
```

**Option B: Let Live Bot Run (Faster)**
```bash
# Just run live bot, it logs everything
python pm_fast_bot.py --duration 12

# After 20+ trades, analyze the logs
```

**Option C: Both (Best)**
- Run live bot now (makes real money)
- Run collector in parallel (gathers backtest data)
- Optimize after you have both live results + historical data

---

## 📊 Expected Timeline

**Week 1:** Run with current params (85-95c, 120s, 70%)  
**Week 2:** Collect 100+ trades, analyze  
**Week 3:** Optimize to best params  
**Week 4:** Validate optimized strategy

If strategy works, you'll see exponential growth! If not, backtesting will reveal exactly what to fix.

Want me to start the data collector in parallel with the live bot?

