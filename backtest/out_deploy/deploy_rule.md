# Final Deployment Rule

## Entry

1. **Trigger**: First tick where UP >= 90c OR DOWN >= 90c
2. **Wait**: 10 seconds
3. **SPIKE Validation** (in those 10 seconds):
   - `min_side >= 88c` (no dump)
   - `max_side >= 93c` (push-through)
4. **Execute**: LIMIT BUY at 93c
   - Cancel if not filled within 2 seconds
   - Conservative backtest assumes `slip_entry = +1c`

## Exit

Check every tick after entry:

1. **TAKE PROFIT**: Exit when `side >= 97c`
   - Limit sell at 97c (no slippage)

2. **STOP LOSS**: Exit when `side <= 86c`
   - Market/aggressive limit sell
   - Assume `slip_exit = +1c` worst-case
   - **This caps max loss to ~9% on invested capital**

3. **Settlement**: If neither TP nor SL hit, hold to settlement

## Dynamic SL (Optional Enhancement)

If `side >= 95c` at any point, raise SL from 86 to 90.
This locks in gains and prevents "nearly won then dumped" scenarios.

## Expected Performance (SPIKE_SL86_TP97, slip_entry=1, slip_exit=1)

| Metric | Value |
|--------|-------|
| Trades | 1461 |
| EV/invested | +2.95% |
| Worst Loss | -35.16% |
| Worst 1% Loss | -12.09% |
| Max DD (@2%) | 0.88% |
| Max DD (@3%) | 1.31% |
| Profit Factor | 2.59 |
| Sharpe Proxy | 0.477 |

## Position Sizing

- **Start**: 2% bankroll per trade
- **After 300+ live trades with verified stats**: increase to 3%
- **Never exceed**: 5% per trade

## Exit Reason Distribution

| Reason | Count | % | Avg PnL |
|--------|-------|---|---------|
| TAKE_PROFIT | 1056 | 72.3% | +6.20% |
| STOP_LOSS | 355 | 24.3% | -7.63% |
| SETTLEMENT_WIN | 50 | 3.4% | +9.42% |
