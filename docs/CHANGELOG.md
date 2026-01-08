# Changelog

## V12 - Production Safety (2026-01-08)

### Files Changed
- `scripts/mm_live_verify_once.py` - Complete rewrite with V12 safety

### Fixes Applied
1. **Trade Ingestion Boundary**: 
   - `window_start_ts = time.time() - 2`
   - Ignore trades before boundary
   - Filter by market token IDs only

2. **Regime + Rebate Viability**:
   - Strict mid range: [0.40, 0.60] for verification
   - Spread <= 3c
   - Volatility <= 8c
   - Time to end >= 180s
   - Rebate must cover adverse budget (2c/share)

3. **Cap Enforcement (Real)**:
   - MAX_SHARES = 3 (hard cap)
   - Entry size clamped to min(QUOTE_SIZE, MAX_SHARES)
   - Cap check after every fill
   - Breach -> cancel entries, EXIT_ONLY

4. **Exit Management (Automatic)**:
   - Ladder: entry+1c -> entry -> entry-1c -> entry-2c -> bid (emergency)
   - Reprice every 5s
   - Taker exit only in last 60s

5. **Stop Conditions**:
   - BALANCE_ERROR on exit
   - Missing txHash
   - EXIT without matching entry (code 3)

### Exit Codes
- 0 = PASS (complete round-trip)
- 1 = FAIL (safety violation)
- 2 = NO_TRADE_SAFE (correctly refused)
- 3 = STATE_DESYNC (boundary violation)

---

## V11 - Fill Tracking Fix (2026-01-08)

### Issues Identified
- Trade API uses `transactionHash`, not `id`
- MAX_SHARES=3 didn't prevent 6-share fill
- Verifier exited early without exit management
- Traded at extreme odds (0.15)

---

## V10 - Failed Fill Tracking (2026-01-08)

### Root Cause
- `trade_id=unknown` because API field name wrong
- Lost $3.52 on 22-share position
