# Corrected Auto-Redemption Implementation

## The Critical Fix

### ❌ WRONG (Original Implementation)
```python
# Direct redemption from EOA - FAILS because EOA doesn't hold positions!
tx = ctf.functions.redeemPositions(...).build_transaction({
    'from': eoa_address  # ❌ EOA has no position tokens!
})
signed = eoa.sign_transaction(tx)
send_transaction(signed)  # No positions to redeem, or payout goes to EOA
```

### ✅ CORRECT (Fixed Implementation)
```python
# Step 1: Build calldata for CTF.redeemPositions
redeem_calldata = ctf.encodeABI(
    fn_name="redeemPositions",
    args=[usdc, parent_collection, condition_id, [1, 2]]
)

# Step 2: Tell PROXY to execute that call
tx = proxy.functions.execute(
    ctf_address,      # target: CTF contract
    0,                # value: 0 ETH
    redeem_calldata   # data: redeemPositions call
).build_transaction({
    'from': eoa_address  # ✅ EOA signs, but PROXY executes
})

signed = eoa.sign_transaction(tx)
send_transaction(signed)

# Result: PROXY calls CTF.redeemPositions, receives USDC payout
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ EOA (Private Key)                                       │
│ - Signs transactions                                    │
│ - Controls proxy wallet                                 │
└────────────┬────────────────────────────────────────────┘
             │
             │ signs tx
             ▼
┌─────────────────────────────────────────────────────────┐
│ Polymarket Proxy Wallet (Smart Contract)               │
│ - Holds USDC balance                                    │
│ - Holds ERC1155 position tokens (Up/Down shares)        │
│ - Can execute arbitrary calls via execute()             │
└────────────┬────────────────────────────────────────────┘
             │
             │ executes call
             ▼
┌─────────────────────────────────────────────────────────┐
│ Conditional Tokens Framework (CTF)                      │
│ - Burns position tokens from msg.sender (= proxy)       │
│ - Pays USDC collateral to msg.sender (= proxy)          │
└─────────────────────────────────────────────────────────┘
```

---

## Key Insight: `msg.sender` Must Hold Positions

**CTF `redeemPositions` behavior:**
1. Burns ERC1155 position tokens from `msg.sender`
2. Calculates payout based on which outcome won
3. Sends USDC collateral to `msg.sender`

**If EOA calls directly:**
- `msg.sender` = EOA
- But EOA has **no position tokens** (they're in the proxy!)
- Transaction either reverts or does nothing

**If Proxy.execute() calls CTF:**
- `msg.sender` = Proxy (the proxy contract itself)
- Proxy **does hold position tokens**
- Positions burn correctly
- USDC payout goes to proxy ✅

---

## Implementation in `pm_fast_bot.py`

### 1. Proxy Contract Interface
```python
# Minimal ABI for Polymarket proxy wallet
proxy_abi = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"}
        ],
        "name": "execute",
        "outputs": [{"name": "", "type": "bytes"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

self.proxy_contract = self.w3.eth.contract(
    address=Web3.to_checksum_address(self.proxy),
    abi=proxy_abi
)
```

### 2. Redemption Method
```python
def redeem_position(self, condition_id: str) -> bool:
    # Build calldata for CTF.redeemPositions
    redeem_calldata = self.ctf.encodeABI(
        fn_name="redeemPositions",
        args=[usdc_addr, parent_collection, condition_bytes, [1, 2]]
    )
    
    # Build tx to Proxy.execute(CTF, 0, calldata)
    tx = self.proxy_contract.functions.execute(
        ctf_addr,        # Call CTF
        0,               # No ETH
        redeem_calldata  # With this data
    ).build_transaction({
        'from': self.account.address,  # EOA signs
        'nonce': nonce,
        'gas': 400000,
        'gasPrice': self.w3.eth.gas_price,
        'chainId': 137
    })
    
    # Sign and send
    signed_tx = self.account.sign_transaction(tx)
    tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
    
    # Wait for confirmation
    receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    
    return receipt['status'] == 1
```

### 3. Balance Reading (Already Correct)
```python
def get_balance(self) -> float:
    # Read from PROXY address (not EOA)
    bal = self.usdc.functions.balanceOf(
        Web3.to_checksum_address(self.proxy)  # ✅ Proxy address
    ).call()
    return bal / 1e6
```

---

## Transaction Flow

### Before Fix (WRONG)
```
User signs tx → CTF.redeemPositions
                 ↓
           "msg.sender = EOA"
                 ↓
     No position tokens to burn!
                 ↓
          Transaction fails
```

### After Fix (CORRECT)
```
User signs tx → Proxy.execute(CTF, 0, calldata)
                     ↓
            Proxy calls CTF.redeemPositions
                     ↓
              "msg.sender = Proxy"
                     ↓
       Proxy holds position tokens ✅
                     ↓
            Tokens burn, USDC payout
                     ↓
           USDC sent to Proxy ✅
                     ↓
      Bot reads proxy USDC balance ✅
```

---

## Verification Steps

### 1. Check Position Tokens (Before Redemption)
```python
# ERC1155 balance check (not implemented but good for debugging)
position_balance = erc1155.balanceOf(proxy_address, position_token_id)
# Should be > 0 if we hold a position
```

### 2. Redeem Via Proxy
```python
success = redeem_position(condition_id)
# Sends: EOA → Proxy → CTF
```

### 3. Check Balance (After Redemption)
```python
new_balance = get_balance()  # Reads from proxy
# Should increase by (shares * payout_per_share)
```

---

## Why This Was Hard to Catch

1. **EOA can send the transaction** - no error at signing/sending level
2. **Transaction might succeed** - if EOA has no positions, redemption is a no-op (doesn't revert)
3. **Balance doesn't update** - USDC goes nowhere or to wrong address
4. **Looks like "API delay"** - bot thinks redemption will happen eventually

The fix ensures:
- ✅ Positions are actually redeemed (proxy holds them)
- ✅ USDC payout goes to the right place (proxy wallet)
- ✅ Bot sees updated balance immediately (reading from proxy)

---

## Files Updated

1. **`pm_fast_bot.py`** - Main bot with proxy-based redemption
2. **`pm_proxy_redeem.py`** - Standalone test script for manual redemption

Both now correctly execute redemption through the proxy wallet.

