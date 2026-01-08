# Changelog

## V6 (2026-01-08) - Anti-Pyramiding + Continuous Mode

### Critical Fixes
- **15s Entry Cooldown**: Prevents multiple orders before fill confirmation
- **Track All Entry Orders**: Maintains set of all posted entry order IDs
- **Cancel-Confirm Exits**: Always cancel existing exit before posting new
- **avgPrice Fallback**: Uses current book price when REST returns 0

### New Features
- **Continuous Mode** (`scripts/mm_continuous.py`):
  - Auto-discovers new 15-min windows
  - Transitions automatically between windows
  - Reports PnL per window and session total
- **Dynamic Position Sizing**: 15% of account balance (min $1.50, max $10)

### Fixes
- Fixed pyramiding bug where 4 orders would fill (20 shares instead of 5)
- Fixed exit pricing when avgPrice=0 (was posting at $0.02 instead of $0.43)
- Fixed BALANCE_ERROR spam by refreshing positions before retry

---

## V5 (2026-01-07) - Opening Mode + Time-Based Exits

### Features
- **Opening Mode**: Special handling for first 30s of window
- **Time-Based Exit Ladder**:
  - T=0-20s: Exit at entry + 2c (TP)
  - T=20-40s: Exit at entry (scratch)
  - T=40s+: Cross spread if emergency enabled

### Fixes
- Replaced price-based stop-loss with time-based ladder
- Added volatility filter (max 12c in 5s window)
- Widened regime filter to 0.30-0.70

---

## V4 (2026-01-06) - Position Reconciliation

### Features
- REST position reconciliation every 0.5-2s
- Synthetic entry creation on missed fills
- Global inventory gate (no entries if inv > 0)

### Issues Found
- Pyramiding: Multiple orders getting filled
- Exit repricing posting duplicate SELLs
- Slow fill detection causing stale state

---

## V3 (2026-01-05) - Endgame Rules

### Features
- Entry cutoff at 3 min to settlement
- Flatten deadline at 2 min to settlement
- Emergency taker exit option
- Spike detection with cooldown

---

## V2 (2026-01-04) - Exit Management

### Features
- Exit supervisor with repricing
- Cancel-confirm-post pattern
- Dust mode for < MIN_SHARES positions

---

## V1 (2026-01-03) - Initial Bot

### Features
- Basic quoting at best_bid
- PostOnly orders for maker rebates
- Simple inventory tracking
