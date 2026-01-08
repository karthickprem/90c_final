# Complete Auto-Redemption Implementation Guide

## Executive Summary

**Problem:** Polymarket positions are held in proxy wallets, not EOA. Different wallet types require different redemption methods.

**Solution:** Universal redemption that detects wallet type and routes to appropriate backend.

---

## Two Wallet Types on Polymarket

### 1. **Gnosis Safe** (Common for integrations)
- Uses `execTransaction(...)` method
- Requires EIP-712 transaction signing
- More complex plumbing

### 2. **Custom Polymarket Proxy** (Magic Link / Email signup)
- Uses simple `execute(...)` method
- Simpler implementation
- Our fixed code assumes this initially

---

## Auto-Detection Flow

```python
def _detect_wallet_type():
    # Test 1: Try Gnosis Safe methods
    try:
        safe.functions.getOwners().call()
        return "safe"  # ✓ It's a Safe
    except:
        pass
    
    # Test 2: Check for Custom Proxy execute() selector
    code = w3.eth.get_code(proxy_address)
    if execute_selector in code:
        return "custom"  # ✓ It's custom proxy
    
    return "unknown"
```

---

## Implementation Files

### 1. **`detect_wallet_type.py`**
Standalone utility to detect your wallet type.

**Usage:**
```bash
python detect_wallet_type.py
```

**Output:**
```
Wallet type: CUSTOM
Your wallet is a Custom Polymarket Proxy.
Redemption must use: execute(...)
```

---

### 2. **`pm_redemption.py`**
Universal redemption module supporting both wallet types.

**Features:**
- Auto-detects wallet type
- Implements both Custom Proxy and Gnosis Safe backends
- Standalone testing capability

**Usage:**
```bash
python pm_redemption.py <conditionId>
```

**Example:**
```bash
python pm_redemption.py 0x4a629eb456c10ea56e4819f5b54c6727be8010b03c31375fed5b9f100f0dee53
```

---

### 3. **`pm_fast_bot.py`** (Updated)
Main trading bot with universal redemption integrated.

**Changes:**
1. Auto-detects wallet type on startup
2. Routes redemption to appropriate method
3. Logs wallet type for transparency

---

## Redemption Methods

### Custom Proxy (Simple)

```python
# Build CTF redemption calldata
redeem_calldata = ctf.encodeABI(
    fn_name="redeemPositions",
    args=[usdc, parent_collection, condition_id, [1, 2]]
)

# Tell proxy to execute
tx = proxy.functions.execute(
    ctf_address,     # target
    0,               # value
    redeem_calldata  # data
).build_transaction({...})
```

**Flow:**
```
EOA signs → Proxy.execute() → CTF.redeemPositions() → USDC to proxy
```

---

### Gnosis Safe (Complex)

```python
# Build CTF redemption calldata
redeem_calldata = ctf.encodeABI(...)

# Get Safe nonce
safe_nonce = safe.functions.nonce().call()

# Build transaction hash for signing
tx_hash = solidityKeccak([...Safe tx params...])

# Sign with EOA
signature = account.signHash(tx_hash)

# Call Safe.execTransaction
tx = safe.functions.execTransaction(
    to=ctf_address,
    value=0,
    data=redeem_calldata,
    operation=0,  # CALL
    ...
    signatures=signature
).build_transaction({...})
```

**Flow:**
```
EOA signs → Safe.execTransaction() → CTF.redeemPositions() → USDC to proxy
```

---

## Settlement Timeline

### Why 2-Minute Delay Exists

**Market closes** → Oracle must report payout → CTF records it on-chain → Redemption possible

**Timeline:**
1. **T+0s:** 15-min window ends
2. **T+30-120s:** Oracle reports payout to CTF
3. **T+120s:** `payoutDenominator(conditionId) > 0` ✓
4. **T+120s+:** Redemption transactions can succeed

**Bot advantage:**
- Polymarket UI waits for backend confirmation before showing "Claim"
- **Your bot** can fire redemption **instantly** when `payoutDenominator > 0`
- Saves 5-30 seconds vs manual clicking

---

## Complete Bot Flow (Updated)

```
1. Window closes
   ↓
2. Bot checks: payoutDenominator(conditionId) > 0
   ↓ (waits 30-120s typically)
3. Condition resolves on-chain ✓
   ↓
4. Bot detects wallet type
   ↓
5. Routes to appropriate redemption method
   ├─→ Custom Proxy: execute(CTF, 0, calldata)
   └─→ Gnosis Safe: execTransaction(...)
   ↓
6. Transaction confirms
   ↓
7. USDC credited to proxy wallet
   ↓
8. Bot reads updated balance
   ↓
9. Continues trading with compounded balance
```

---

## Testing Your Setup

### Step 1: Detect Wallet Type
```bash
python detect_wallet_type.py
```

**Expected output:**
```
✓ Custom Proxy detected!
  Has execute() method

RESULT: CUSTOM
```

or

```
✓ Gnosis Safe detected!
  Owners: ['0x...']
  Threshold: 1

RESULT: SAFE
```

---

### Step 2: Test Redemption (If You Have Resolved Position)

Get a `conditionId` from a completed trade:
```bash
python pm_redemption.py 0x4a629eb...
```

**Expected output (success):**
```
Wallet type: CUSTOM
Resolved on-chain: True

Redeeming via Custom Proxy...
Tx sent: 0xabc123...
✓ Redemption successful! Gas used: 123456

Result: {'success': True, 'tx_hash': '0xabc...', 'method': 'custom_proxy'}
```

---

### Step 3: Run Bot
```bash
python pm_fast_bot.py --duration 12
```

**Expected startup:**
```
POLYMARKET FAST BOT
Proxy: 0x3C008F983...
Wallet type: CUSTOM  ← Confirms detection
Starting balance: $15.77
```

---

## Key Differences: Safe vs Custom

| Feature | Custom Proxy | Gnosis Safe |
|---------|--------------|-------------|
| **Method** | `execute(to, value, data)` | `execTransaction(to, value, data, ...)` |
| **Signature** | Simple EOA sign | EIP-712 Safe tx hash |
| **Complexity** | Low | High |
| **Gas** | ~300-400k | ~500k |
| **Common For** | Magic Link / Email signups | Advanced users / integrations |

---

## Troubleshooting

### "Unknown wallet type"
- Check proxy address is correct
- Verify RPC connection to Polygon
- Run `detect_wallet_type.py` for diagnostics

### "Redemption failed"
- Market may not be resolved yet (`payoutDenominator` still 0)
- Check you actually hold a position in that market
- Verify gas price is sufficient

### "Balance not updating after redemption"
- Wait 1-2 blocks (2-5 seconds on Polygon)
- Check transaction actually succeeded (status = 1)
- Verify reading balance from **proxy address**, not EOA

---

## Files Summary

| File | Purpose | When to Use |
|------|---------|-------------|
| `detect_wallet_type.py` | Detect Safe vs Custom | First time setup |
| `pm_redemption.py` | Universal redemption module | Testing / Manual redemption |
| `pm_fast_bot.py` | Main trading bot | Automated trading |
| `COMPLETE_REDEMPTION_GUIDE.md` | This guide | Reference |

---

## Next Steps

1. ✅ Run `detect_wallet_type.py` to confirm your wallet type
2. ✅ If you have a resolved position, test with `pm_redemption.py`
3. ✅ Run `pm_fast_bot.py` for automated trading
4. ✅ Monitor first redemption to verify it works end-to-end

**The bot is now truly autonomous** - no manual claims needed! 🎯

