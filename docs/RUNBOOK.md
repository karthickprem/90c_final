# MM Bot Production Runbook

## Quick Reference

### Run Modes

| Mode | Command | Risk |
|------|---------|------|
| DRYRUN | `python scripts/mm_continuous.py` | None |
| VERIFY | `LIVE=1 python scripts/mm_live_verify_once.py` | Low ($1.50 max) |
| LIVE | `LIVE=1 MM_EXIT_ENFORCED=1 python scripts/mm_continuous.py` | Full |

### Before ANY LIVE Run

1. **Check account status:**
   ```powershell
   python check_status.py
   ```

2. **Run verification test:**
   ```powershell
   $env:LIVE="1"; python scripts/mm_live_verify_once.py
   ```

3. **Only proceed if verification PASSES**

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| LIVE | 0 | Enable real trading |
| MM_EXIT_ENFORCED | 0 | Required for LIVE mode |
| MM_EMERGENCY_TAKER_EXIT | 0 | Allow taker exits on flatten |
| MM_MAX_USDC_LOCKED | 2.50 | Max exposure per position |
| MM_QUOTE_SIZE | 6 | Shares per entry order |

### Logs That Prove Correctness

1. **Fill tracking works:**
   ```
   [FILL] ENTRY: BUY 6.00 @ 0.4500 (MAKER) trade_id=0xabc123...
   [FILL] EXIT: SELL 6.00 @ 0.4600 (MAKER) trade_id=0xdef456...
   [ROUND-TRIP] Entry=0.4500 Exit=0.4600 Size=6.00 PnL=+$0.06 (from fills)
   ```

2. **No synthetic entries:**
   - Should NEVER see: `[FILL] SYNTHETIC ENTRY`

3. **Exposure capped:**
   - Should see: `[BLOCKED] Exposure ${X} > MAX ${Y}`

4. **Safety checks work:**
   - Should see: `[CLEANUP] All orders cancelled`
   - Should see: `[SAFETY] ...` only if violations occur

### Emergency Procedures

#### Stop Trading Immediately
```powershell
taskkill /F /IM python.exe
```

#### Cancel All Orders
```powershell
python -c "from mm_bot.config import Config; from mm_bot.clob import ClobWrapper; c = Config.from_env(); ClobWrapper(c).cancel_all()"
```

#### Check Current Positions
```powershell
python check_status.py
```

### Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| trade_id=unknown | API not returning txHash | Wait, API issue |
| BALANCE_ERROR on SELL | Shares locked in other order | Cancel all, retry |
| Exposure exceeded | Multiple fills before cancel | Lower QUOTE_SIZE |
| No fills | Market at extreme odds | Wait for better market |

