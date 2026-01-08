# MM Bot State Machine (V11)

## States

```
IDLE -> ENTRY_PENDING -> POSITION_OPEN -> EXIT_PENDING -> IDLE
                |               |
                v               v
           (cancelled)     (emergency exit)
```

### State Definitions

| State | Description | Allowed Actions |
|-------|-------------|-----------------|
| IDLE | No inventory, no pending orders | Post entry order |
| ENTRY_PENDING | Entry order posted, waiting for fill | Cancel entry, wait |
| POSITION_OPEN | Confirmed BUY fill, holding shares | Post exit order |
| EXIT_PENDING | Exit order posted, waiting for fill | Reprice exit, wait |

## State Transitions

### IDLE -> ENTRY_PENDING
- **Trigger:** Entry order posted successfully
- **Conditions:**
  - No confirmed inventory (from fills)
  - No pending entry orders
  - Exposure < MAX_USDC_LOCKED
  - Market in valid regime

### ENTRY_PENDING -> IDLE
- **Trigger:** Entry order cancelled (stale, regime change)
- **Conditions:**
  - Cancel confirmed via API

### ENTRY_PENDING -> POSITION_OPEN
- **Trigger:** BUY fill detected from trades API
- **Conditions:**
  - Fill has valid trade_id (not unknown)
  - Fill token matches our market

### POSITION_OPEN -> EXIT_PENDING
- **Trigger:** Exit order posted
- **Conditions:**
  - Confirmed inventory > 0

### EXIT_PENDING -> IDLE
- **Trigger:** SELL fill detected from trades API
- **Conditions:**
  - Fill closes entire position

### EXIT_PENDING -> POSITION_OPEN
- **Trigger:** Exit order cancelled for repricing
- **Conditions:**
  - Still have confirmed inventory

## Event Sources

| Event | Source | Reliability |
|-------|--------|-------------|
| BUY fill | Trades API (poll) | High |
| SELL fill | Trades API (poll) | High |
| Order posted | API response | High |
| Order cancelled | API response | High |
| Position change | REST positions | LOW - sanity only |

## Safety Rules

1. **Fills are SOURCE OF TRUTH**
   - Position open ONLY from confirmed BUY fill
   - Position close ONLY from confirmed SELL fill
   - REST positions endpoint is sanity-check only

2. **No Pyramiding**
   - If confirmed inventory > 0, no new entries
   - If entry order pending, no new entries
   - Max 1 entry order at a time

3. **Exposure Cap**
   - confirmed_shares * mid + pending_entry_cost <= MAX_USDC_LOCKED
   - If exceeded: cancel entries, enter EXIT_ONLY mode

4. **Order Management**
   - Max 1 entry order at a time
   - Max 1 exit order per token
   - Cancel-confirm-post for repricing

## STOP Conditions (Emergency)

Immediately cancel all orders and stop trading if:
- trade_id missing/unknown
- REST positions flapping without trades
- Exposure > MAX_USDC_LOCKED
- BALANCE_ERROR on SELL
- open_orders > allowed

