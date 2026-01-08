"""
POLYMARKET FAST BOT
===================
- Ultra-fast polling (50ms)
- Immediate order at current ask when signal triggers
- Read ACTUAL fill details from API
- Read ACTUAL balance from blockchain
"""

import json
import time
import asyncio
import aiohttp
from datetime import datetime
from typing import Optional, Dict
from web3 import Web3
from eth_account import Account

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from py_clob_client.constants import POLYGON

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# === CONFIG ===
ENTRY_MIN = 0.85         # Entry range: 85c to 99c
ENTRY_MAX = 0.99         # Upper bound (high probability)
ENTRY_WINDOW_SECS = 180  # ONLY enter during LAST 3 minutes (180s)
BALANCE_PCT = 0.70       # Use 70% of balance (temporary for low balance)
MIN_SHARES = 5
POLL_MS = 50             # Ultra fast: 50ms (20 checks/sec)
SETTLE_WAIT = 120


class FastBot:
    def __init__(self):
        with open("pm_api_config.json") as f:
            config = json.load(f)
        
        self.proxy = config.get("proxy_address", "")
        
        creds = ApiCreds(
            api_key=config["api_key"],
            api_secret=config["api_secret"],
            api_passphrase=config["api_passphrase"],
        )
        self.client = ClobClient(
            host=CLOB_HOST,
            key=config["private_key"],
            chain_id=POLYGON,
            creds=creds,
            signature_type=1,
            funder=self.proxy,
        )
        
        # Web3 for balance
        self.w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        usdc_addr = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        usdc_abi = [{"constant":True,"inputs":[{"name":"account","type":"address"}],
                     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
        self.usdc = self.w3.eth.contract(address=Web3.to_checksum_address(usdc_addr), abi=usdc_abi)
        
        # Async session (reuse for speed)
        self.session = None
        
        # CTF contract for auto-redemption
        ctf_addr = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        ctf_abi = [
            {"inputs": [{"name": "conditionId", "type": "bytes32"}],
             "name": "payoutDenominator", "outputs": [{"name": "", "type": "uint256"}],
             "stateMutability": "view", "type": "function"},
            {"inputs": [{"name": "collateralToken", "type": "address"},
                        {"name": "parentCollectionId", "type": "bytes32"},
                        {"name": "conditionId", "type": "bytes32"},
                        {"name": "indexSets", "type": "uint256[]"}],
             "name": "redeemPositions", "outputs": [],
             "stateMutability": "nonpayable", "type": "function"}
        ]
        self.ctf = self.w3.eth.contract(address=Web3.to_checksum_address(ctf_addr), abi=ctf_abi)
        self.account = Account.from_key(config["private_key"])
        
        # Detect wallet type
        self.wallet_type = self._detect_wallet_type()
        self.log(f"Wallet type: {self.wallet_type.upper()}")
        
        if self.wallet_type == "unknown":
            self.log(f"WARNING: Will try both redemption methods")
        
        # State
        self.tokens = {}
        self.current_window = None
        self.current_condition_id = None  # For auto-redemption
        self.traded_this_window = False
        
        # Position - ACTUAL values only
        self.holding_side = None
        self.holding_price = 0
        self.holding_shares = 0
        self.holding_cost = 0
        self.order_id = None
        
        # Stats
        self.wins = 0
        self.losses = 0
        self.trades = []
        self.starting_balance = 0
        
        # Balance cache (to avoid excessive RPC calls)
        self.balance_cache = 0
        self.last_balance_check = 0
    
    def get_balance(self) -> float:
        """Read TOTAL portfolio balance (cash + unredeemed positions)"""
        now = time.time()
        if now - self.last_balance_check > 30 or self.balance_cache == 0:
            try:
                # Get cash balance from blockchain
                bal = self.usdc.functions.balanceOf(Web3.to_checksum_address(self.proxy)).call()
                cash = bal / 1e6
                
                # Get position value from Polymarket Data API
                position_value = 0
                try:
                    import requests
                    r = requests.get(
                        f"https://data-api.polymarket.com/value",
                        params={"user": self.proxy},
                        timeout=5
                    )
                    if r.status_code == 200:
                        data = r.json()
                        # API returns: [{'user': '0x...', 'value': 6.05}]
                        if isinstance(data, list) and len(data) > 0:
                            position_value = float(data[0].get("value", 0))
                except:
                    pass  # Position value stays 0
                
                # Total portfolio = cash + positions
                self.balance_cache = cash + position_value
                self.last_balance_check = now
                
            except Exception as e:
                # On error, keep existing cache (don't return 0!)
                if self.balance_cache == 0:
                    self.log(f"Balance read error: {str(e)[:60]}")
        
        return self.balance_cache
    
    def _detect_wallet_type(self) -> str:
        """Auto-detect Gnosis Safe vs Custom Proxy"""
        # Try Safe methods
        try:
            safe_abi = [{"inputs": [], "name": "getOwners", "outputs": [{"name": "", "type": "address[]"}],
                        "stateMutability": "view", "type": "function"}]
            safe = self.w3.eth.contract(address=Web3.to_checksum_address(self.proxy), abi=safe_abi)
            safe.functions.getOwners().call()
            return "safe"
        except:
            pass
        
        # Try Custom Proxy
        try:
            code = self.w3.eth.get_code(Web3.to_checksum_address(self.proxy))
            execute_selector = Web3.keccak(text="execute(address,uint256,bytes)")[:4]
            if execute_selector in code:
                return "custom"
        except:
            pass
        
        return "unknown"
    
    def is_resolved_onchain(self, condition_id: str) -> bool:
        """Check if market is resolved on-chain (not just API)"""
        try:
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            payout_denom = self.ctf.functions.payoutDenominator(condition_bytes).call()
            return payout_denom > 0
        except:
            return False
    
    def redeem_position(self, condition_id: str) -> bool:
        """Auto-redeem resolved position VIA PROXY WALLET"""
        if self.wallet_type == "custom":
            return self._redeem_via_custom_proxy(condition_id)
        elif self.wallet_type == "safe":
            return self._redeem_via_safe(condition_id)
        else:
            # Unknown wallet type - try custom first (most common)
            self.log(f"  Unknown wallet type - trying custom proxy method...")
            if self._redeem_via_custom_proxy(condition_id):
                return True
            
            self.log(f"  Custom failed, trying Safe method...")
            if self._redeem_via_safe(condition_id):
                return True
            
            self.log(f"  Both methods failed - manual claim needed")
            return False
    
    def _redeem_via_custom_proxy(self, condition_id: str) -> bool:
        """Redeem via Custom Proxy (Magic Link wallets)"""
        try:
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            parent_collection = Web3.to_bytes(hexstr="0x" + "00" * 32)
            usdc_addr = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
            ctf_addr = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
            
            # Build CTF redemption calldata
            redeem_calldata = self.ctf.encodeABI(
                fn_name="redeemPositions",
                args=[usdc_addr, parent_collection, condition_bytes, [1, 2]]
            )
            
            # Build proxy contract interface
            custom_abi = [
                {"inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}, {"name": "data", "type": "bytes"}],
                 "name": "execute", "outputs": [{"name": "", "type": "bytes"}],
                 "stateMutability": "nonpayable", "type": "function"}
            ]
            proxy = self.w3.eth.contract(address=Web3.to_checksum_address(self.proxy), abi=custom_abi)
            
            # Call Proxy.execute(CTF, 0, calldata)
            nonce = self.w3.eth.get_transaction_count(self.account.address)
            
            tx = proxy.functions.execute(
                ctf_addr, 0, redeem_calldata
            ).build_transaction({
                'from': self.account.address,
                'nonce': nonce,
                'gas': 400000,
                'gasPrice': self.w3.eth.gas_price,
                'chainId': 137
            })
            
            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            self.log(f"  Tx: {tx_hash.hex()[:40]}...")
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            success = (receipt['status'] == 1)
            
            if success:
                self.log(f"  [OK] Redeemed via custom proxy! Gas: {receipt['gasUsed']}")
            else:
                self.log(f"  [FAIL] Redemption tx failed")
            
            return success
            
        except Exception as e:
            self.log(f"  Custom proxy error: {str(e)[:60]}")
            return False
    
    def _redeem_via_safe(self, condition_id: str) -> bool:
        """Redeem via Gnosis Safe (1-of-1 owner)"""
        try:
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            parent_collection = Web3.to_bytes(hexstr="0x" + "00" * 32)
            usdc_addr = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
            ctf_addr = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
            
            # Build CTF redemption calldata
            redeem_calldata = self.ctf.encodeABI(
                fn_name="redeemPositions",
                args=[usdc_addr, parent_collection, condition_bytes, [1, 2]]
            )
            
            # Build Safe contract interface
            safe_abi = [
                {"inputs": [], "name": "nonce", "outputs": [{"name": "", "type": "uint256"}],
                 "stateMutability": "view", "type": "function"},
                {"inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
                            {"name": "data", "type": "bytes"}, {"name": "operation", "type": "uint8"},
                            {"name": "safeTxGas", "type": "uint256"}, {"name": "baseGas", "type": "uint256"},
                            {"name": "gasPrice", "type": "uint256"}, {"name": "gasToken", "type": "address"},
                            {"name": "refundReceiver", "type": "address"}, {"name": "signatures", "type": "bytes"}],
                 "name": "execTransaction", "outputs": [{"name": "success", "type": "bool"}],
                 "stateMutability": "payable", "type": "function"}
            ]
            safe = self.w3.eth.contract(address=Web3.to_checksum_address(self.proxy), abi=safe_abi)
            
            # Get Safe nonce
            safe_nonce = safe.functions.nonce().call()
            
            # Build Safe transaction hash
            tx_hash_data = self.w3.solidityKeccak(
                ['address', 'uint256', 'bytes', 'uint8', 'uint256', 'uint256',
                 'uint256', 'address', 'address', 'uint256'],
                [ctf_addr, 0, redeem_calldata, 0, 0, 0,
                 0, "0x" + "00" * 20, "0x" + "00" * 20, safe_nonce]
            )
            
            # Sign
            signed_msg = self.account.signHash(tx_hash_data)
            signature = signed_msg.r.to_bytes(32, 'big') + \
                       signed_msg.s.to_bytes(32, 'big') + \
                       signed_msg.v.to_bytes(1, 'big')
            
            # Call Safe.execTransaction
            nonce = self.w3.eth.get_transaction_count(self.account.address)
            
            tx = safe.functions.execTransaction(
                ctf_addr, 0, redeem_calldata, 0, 0, 0,
                0, "0x" + "00" * 20, "0x" + "00" * 20, signature
            ).build_transaction({
                'from': self.account.address,
                'nonce': nonce,
                'gas': 500000,
                'gasPrice': self.w3.eth.gas_price,
                'chainId': 137
            })
            
            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            self.log(f"  Tx: {tx_hash.hex()[:40]}...")
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            success = (receipt['status'] == 1)
            
            if success:
                self.log(f"  [OK] Redeemed via Safe! Gas: {receipt['gasUsed']}")
            else:
                self.log(f"  [FAIL] Redemption tx failed")
            
            return success
            
        except Exception as e:
            self.log(f"  Safe error: {str(e)[:60]}")
            return False
    
    def cancel_all(self):
        try:
            self.client.cancel_all()
        except:
            pass
    
    def get_window(self) -> Dict:
        ts = int(time.time())
        start = ts - (ts % 900)
        end = start + 900
        return {
            "slug": f"btc-updown-15m-{start}",
            "secs_left": end - ts,
            "time_str": f"{(end-ts)//60}:{(end-ts)%60:02d}"
        }
    
    async def fetch_tokens(self, slug: str) -> Dict:
        try:
            async with self.session.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug},
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                if resp.status == 200:
                    markets = await resp.json()
                    if markets:
                        m = markets[0]
                        toks = m.get("clobTokenIds", [])
                        outs = m.get("outcomes", [])
                        cond_id = m.get("conditionId", None)
                        
                        if isinstance(toks, str):
                            toks = json.loads(toks)
                        if isinstance(outs, str):
                            outs = json.loads(outs)
                        
                        # Store conditionId for redemption
                        if cond_id:
                            self.current_condition_id = cond_id
                        
                        return {o.lower(): t for o, t in zip(outs, toks)}
        except:
            pass
        return {}
    
    async def fetch_midpoint(self, token: str) -> float:
        """Fetch midpoint price - SIMPLE like pm_live_simple.py"""
        try:
            async with self.session.get(
                f"{CLOB_HOST}/midpoint",
                params={"token_id": token},
                timeout=aiohttp.ClientTimeout(total=2)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("mid", 0))
        except:
            pass
        return 0
    
    async def fetch_prices_fast(self) -> Dict:
        """Fetch both prices in parallel - SIMPLE like pm_live_simple.py"""
        if not self.tokens:
            return {}
        
        results = {}
        tasks = []
        sides = []
        
        for side, token in self.tokens.items():
            tasks.append(self.fetch_midpoint(token))
            sides.append(side)
        
        try:
            prices = await asyncio.gather(*tasks, return_exceptions=True)
            for side, price in zip(sides, prices):
                if isinstance(price, (int, float)):
                    results[side] = {"mid": price}
                else:
                    results[side] = {"mid": 0}
        except:
            pass
        
        return results
    
    def execute_buy_fast(self, side: str, price: float) -> bool:
        """Execute buy at current price"""
        token = self.tokens.get(side)
        if not token:
            return False
        
        # Read ACTUAL balance
        balance = self.get_balance()
        trade_amount = balance * BALANCE_PCT
        
        shares = trade_amount / price
        if shares < MIN_SHARES:
            self.log(f"  Shares {shares:.1f} < min {MIN_SHARES}")
            return False
        
        self.log(f"  BUYING {side.upper()} @ {price*100:.1f}c")
        self.log(f"    Balance: ${balance:.2f} | Using: ${trade_amount:.2f}")
        self.log(f"    Shares: {shares:.1f}")
        
        try:
            # Place order at current price
            args = OrderArgs(token_id=token, price=price, size=shares, side=BUY)
            signed = self.client.create_order(args)
            result = self.client.post_order(signed, OrderType.GTC)
            
            if result and result.get("success"):
                self.order_id = result.get("orderID")
                self.log(f"    ORDER OK: {self.order_id[:30]}...")
                
                # Read ACTUAL fill details
                time.sleep(1)
                actual = self.get_actual_fill(self.order_id)
                
                if actual:
                    self.holding_side = side
                    self.holding_price = actual["price"]
                    self.holding_shares = actual["shares"]
                    self.holding_cost = actual["cost"]
                    self.log(f"    ACTUAL FILL: {actual['shares']:.1f} @ {actual['price']*100:.1f}c = ${actual['cost']:.2f}")
                    return True
                else:
                    # Fallback - but log warning
                    self.log(f"    WARNING: Could not read actual fill, using estimates")
                    self.holding_side = side
                    self.holding_price = price
                    self.holding_shares = shares
                    self.holding_cost = shares * price
                    return True
            else:
                self.log(f"    ORDER FAILED: {result}")
        except Exception as e:
            self.log(f"    ERROR: {str(e)[:80]}")
        
        return False
    
    def get_actual_fill(self, order_id: str) -> Optional[Dict]:
        """Get ACTUAL fill details from order"""
        try:
            order = self.client.get_order(order_id)
            if order:
                matched = float(order.get("size_matched", 0))
                price = float(order.get("price", 0))
                status = str(order.get("status", "")).upper()
                
                if matched > 0:
                    return {
                        "shares": matched,
                        "price": price,
                        "cost": matched * price,
                        "status": status
                    }
                elif status in ["MATCHED", "FILLED"]:
                    # Use original size if matched not reported
                    original = float(order.get("original_size", 0))
                    return {
                        "shares": original,
                        "price": price,
                        "cost": original * price,
                        "status": status
                    }
        except:
            pass
        return None
    
    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts}] {msg}")
    
    async def get_market_status(self, slug: str) -> Dict:
        """Get market resolution status from Polymarket API"""
        result = {"closed": False, "resolved": False, "winner": None}
        
        try:
            async with self.session.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    markets = await resp.json()
                    if markets:
                        m = markets[0]
                        
                        # Check if closed
                        result["closed"] = m.get("closed", False)
                        
                        # Check resolution status
                        uma_status = m.get("umaResolutionStatus", "")
                        result["resolved"] = (uma_status == "resolved")
                        
                        # Get winning outcome from outcomePrices
                        # Winner has price = "1", loser has price = "0"
                        outcomes = m.get("outcomes", [])
                        outcome_prices = m.get("outcomePrices", [])
                        
                        if isinstance(outcomes, str):
                            outcomes = json.loads(outcomes)
                        if isinstance(outcome_prices, str):
                            outcome_prices = json.loads(outcome_prices)
                        
                        # Find winner (price = "1")
                        # If outcomePrices are [1.0, 0.0] or [0.0, 1.0], market IS resolved
                        for i, price in enumerate(outcome_prices):
                            price_float = float(price)
                            if price_float >= 0.99:
                                if i < len(outcomes):
                                    result["winner"] = outcomes[i].lower()
                                    result["resolved"] = True  # Force resolved if we have a winner price
                                break
                        
        except Exception as e:
            self.log(f"  Market status error: {e}")
        
        return result
    
    async def wait_for_settlement(self, slug: str, max_wait: int = 180) -> Dict:
        """Wait for on-chain resolution and AUTO-REDEEM"""
        start = time.time()
        balance_start = self.get_balance()
        winner = None
        
        # Phase 1: Wait for ON-CHAIN resolution
        print(f"\n  Waiting for on-chain resolution...", flush=True)
        
        resolved_onchain = False
        
        while time.time() - start < max_wait:
            elapsed = int(time.time() - start)
            
            # Check ON-CHAIN resolution (not just API)
            if self.current_condition_id and self.is_resolved_onchain(self.current_condition_id):
                if not resolved_onchain:
                    print(f"\n  [{elapsed}s] RESOLVED ON-CHAIN!")
                    resolved_onchain = True
                
                # Now wait for API to provide winner
                status = await self.get_market_status(slug)
                if status["winner"]:
                    winner = status["winner"]
                    we_won = (winner == self.holding_side)
                    print(f"  [{elapsed}s] Winner: {winner.upper()} | We have: {self.holding_side.upper()} -> {'WIN' if we_won else 'LOSS'}")
                    break
                else:
                    # On-chain resolved but API not updated yet - keep waiting
                    print(f"\r  [{elapsed}s] Waiting for API winner...  ", end="", flush=True)
            else:
                print(f"\r  [{elapsed}s] Checking on-chain...  ", end="", flush=True)
            
            await asyncio.sleep(5)
        
        # If on-chain resolved but no API winner, try redemption anyway!
        if not winner and resolved_onchain:
            print(f"\n  On-chain resolved but API winner unknown - attempting redemption anyway")
        elif not winner:
            print(f"\n  Timeout - market not resolved on-chain after {max_wait}s")
            balance_now = self.get_balance()
            return {
                "resolved": False,
                "winner": None,
                "balance_before": balance_start,
                "balance_after": balance_now
            }
        
        # Phase 2: AUTO-REDEEM (we'll find out if we won from balance change)
        we_won = (winner == self.holding_side) if winner else True  # Assume win and redeem
        
        if we_won:
            print(f"  Attempting AUTO-REDEMPTION...")
            
            if self.current_condition_id:
                success = self.redeem_position(self.current_condition_id)
                
                if success:
                    # Wait a bit for balance to update
                    await asyncio.sleep(3)
                    balance_now = self.get_balance()
                    
                    if balance_now > balance_start + 0.01:
                        profit = balance_now - balance_start
                        print(f"  AUTO-REDEEMED! ${balance_start:.2f} -> ${balance_now:.2f} (+${profit:.2f})")
                    else:
                        print(f"  Redemption tx succeeded but balance not updated yet")
                        # Wait a bit more
                        for i in range(6):
                            await asyncio.sleep(5)
                            balance_now = self.get_balance()
                            if balance_now > balance_start + 0.01:
                                profit = balance_now - balance_start
                                print(f"\n  Balance updated! ${balance_start:.2f} -> ${balance_now:.2f} (+${profit:.2f})")
                                break
                            print(f"\r  Waiting for balance... ${balance_now:.2f}  ", end="", flush=True)
                else:
                    print(f"  Auto-redemption failed - may need manual claim")
                    balance_now = self.get_balance()
            else:
                print(f"  No conditionId - cannot auto-redeem")
                balance_now = balance_start
        else:
            # We lost - no payout expected
            print(f"  We lost - no redemption needed")
            balance_now = self.get_balance()
        
        return {
            "resolved": True,
            "winner": winner,
            "balance_before": balance_start,
            "balance_after": balance_now
        }
    
    async def handle_settlement(self):
        """Wait for settlement, read outcome, verify balance"""
        if not self.holding_side:
            return
        
        self.log(f"=" * 50)
        self.log(f"WINDOW CLOSED: {self.current_window}")
        self.log(f"=" * 50)
        self.log(f"  Our position: {self.holding_side.upper()}")
        self.log(f"  Entry: {self.holding_shares:.1f} shares @ {self.holding_price*100:.1f}c")
        self.log(f"  Cost: ${self.holding_cost:.2f}")
        
        # Wait for resolution and get result
        settle_result = await self.wait_for_settlement(self.current_window, max_wait=SETTLE_WAIT)
        
        winner = settle_result.get("winner")
        balance_before = settle_result.get("balance_before", 0)
        balance_after = settle_result.get("balance_after", 0)
        resolved = settle_result.get("resolved", False)
        
        # Determine if we won
        if resolved and winner:
            we_won = (winner == self.holding_side)
            self.log(f"  Market winner: {winner.upper()}")
        else:
            # Fallback: determine from balance change
            payout = balance_after - balance_before
            we_won = (payout > self.holding_cost * 0.5)
            self.log(f"  Resolution unclear, using balance change")
        
        # Calculate payout
        payout = balance_after - balance_before
        
        if we_won:
            self.wins += 1
            profit = self.holding_shares - self.holding_cost
            self.log(f"")
            self.log(f"  >>> RESULT: WIN! <<<")
            self.log(f"  Payout: ${self.holding_shares:.2f} (1 share = $1)")
            self.log(f"  Profit: +${profit:.2f}")
        else:
            self.losses += 1
            self.log(f"")
            self.log(f"  >>> RESULT: LOSS <<<")
            self.log(f"  Lost: -${self.holding_cost:.2f}")
        
        self.log(f"")
        self.log(f"  Balance: ${balance_before:.2f} -> ${balance_after:.2f}")
        self.log(f"=" * 50)
        
        # Record trade
        self.trades.append({
            "window": self.current_window,
            "side": self.holding_side,
            "entry_price": self.holding_price,
            "shares": self.holding_shares,
            "cost": self.holding_cost,
            "winner": winner,
            "we_won": we_won,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "payout": payout
        })
        
        # Clear position
        self.holding_side = None
        self.holding_price = 0
        self.holding_shares = 0
        self.holding_cost = 0
        self.order_id = None
    
    async def setup_new_window(self, window: Dict):
        self.cancel_all()
        
        self.current_window = window["slug"]
        self.tokens = await self.fetch_tokens(self.current_window)
        self.traded_this_window = False
        
        balance = self.get_balance()
        
        self.log(f"NEW WINDOW: {window['slug']}")
        self.log(f"  Balance: ${balance:.2f}")
        
        min_required = MIN_SHARES * ENTRY_MIN
        if not self.tokens:
            self.log(f"  ERROR: No tokens")
        elif balance < min_required:
            self.log(f"  WARNING: Balance ${balance:.2f} < min ${min_required:.2f}")
    
    async def run(self, duration_hours: float = 12):
        self.log("=" * 60)
        self.log("POLYMARKET FAST BOT - LATE ENTRY")
        self.log("=" * 60)
        self.log(f"Proxy: {self.proxy}")
        self.log(f"Strategy: Buy {ENTRY_MIN*100:.0f}c-{ENTRY_MAX*100:.0f}c in LAST 3 MINUTES")
        self.log(f"Entry window: Last 3:00 to 0:30 (150 sec window)")
        self.log(f"Position size: {BALANCE_PCT*100:.0f}% per trade")
        self.log(f"Poll: {POLL_MS}ms ({1000/POLL_MS:.0f} checks/sec)")
        self.log(f"Duration: {duration_hours}h")
        
        self.cancel_all()
        
        self.starting_balance = self.get_balance()
        self.log(f"Starting balance: ${self.starting_balance:.2f}")
        self.log("=" * 60)
        
        # Create reusable session
        self.session = aiohttp.ClientSession()
        
        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        last_window = None
        last_status = 0
        tick_count = 0
        
        try:
            while time.time() < deadline:
                tick_start = time.time()
                tick_count += 1
                
                window = self.get_window()
                
                # Window transition
                if window["slug"] != last_window:
                    if self.holding_side:
                        await self.handle_settlement()
                    await self.setup_new_window(window)
                    last_window = window["slug"]
                
                # Get prices FAST (parallel fetch) - SIMPLE
                prices = await self.fetch_prices_fast()
                up_data = prices.get("up", {})
                dn_data = prices.get("down", {})
                
                up_mid = up_data.get("mid", 0)
                dn_mid = dn_data.get("mid", 0)
                
                # Entry logic - LATE WINDOW ONLY: Last 3 minutes (180s to 30s)
                if not self.traded_this_window and not self.holding_side and self.tokens:
                    secs = window["secs_left"]
                    
                    # ONLY trade in last 180s to 30s (3 min window)
                    if 30 <= secs <= ENTRY_WINDOW_SECS:
                        
                        # Check UP: 85c <= price <= 95c
                        if ENTRY_MIN <= up_mid <= ENTRY_MAX:
                            self.log(f"*** SIGNAL: UP @ {up_mid*100:.0f}c [T-{secs}s] ***")
                            if self.execute_buy_fast("up", up_mid):
                                self.traded_this_window = True
                        
                        # Check DOWN: 85c <= price <= 95c
                        elif ENTRY_MIN <= dn_mid <= ENTRY_MAX:
                            self.log(f"*** SIGNAL: DOWN @ {dn_mid*100:.0f}c [T-{secs}s] ***")
                            if self.execute_buy_fast("down", dn_mid):
                                self.traded_this_window = True
                
                # Status - update prices every tick (fast)
                hold_str = ""
                if self.holding_side:
                    hold_str = f" | HOLD {self.holding_side.upper()}"
                
                # Get balance (cached, refreshes every 30s)
                current_bal = self.get_balance()
                pnl = current_bal - self.starting_balance
                
                # Show MID prices (updates every 50ms)
                print(f"\r  [{window['time_str']}] UP={up_mid*100:.0f}c DOWN={dn_mid*100:.0f}c{hold_str} | W{self.wins}/L{self.losses} | ${current_bal:.2f} ({pnl:+.2f})  ", end="", flush=True)
                
                # Precise timing
                elapsed = time.time() - tick_start
                sleep_time = max(0, (POLL_MS / 1000) - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
        
        except KeyboardInterrupt:
            self.log("\n\nSTOPPED")
        except Exception as e:
            self.log(f"\n\nERROR: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await self.session.close()
            self.cancel_all()
            self.summary()
    
    def summary(self):
        balance = self.get_balance()
        pnl = balance - self.starting_balance
        
        self.log("\n" + "=" * 60)
        self.log("SUMMARY")
        self.log("=" * 60)
        self.log(f"Starting: ${self.starting_balance:.2f}")
        self.log(f"Final:    ${balance:.2f}")
        self.log(f"P&L:      ${pnl:+.2f}")
        self.log(f"Wins:     {self.wins}")
        self.log(f"Losses:   {self.losses}")
        
        with open("pm_fast_results.json", "w") as f:
            json.dump({
                "starting": self.starting_balance,
                "final": balance,
                "pnl": pnl,
                "wins": self.wins,
                "losses": self.losses,
                "trades": self.trades
            }, f, indent=2)


class PaperBot:
    """Paper trading to test strategy without risk"""
    
    def __init__(self):
        self.session = None
        self.tokens = {}
        self.current_slug = None
        
        # Paper state
        self.paper_trades = []
        self.balance = 10.0  # Start with $10 paper money
        self.starting_balance = 10.0
        self.wins = 0
        self.losses = 0
    
    def get_window(self):
        ts = int(time.time())
        start = ts - (ts % 900)
        end = start + 900
        return {
            "slug": f"btc-updown-15m-{start}",
            "secs_left": end - ts,
            "time_str": f"{(end-ts)//60}:{(end-ts)%60:02d}"
        }
    
    async def fetch_tokens(self, slug):
        try:
            async with self.session.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug},
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                if resp.status == 200:
                    markets = await resp.json()
                    if markets:
                        m = markets[0]
                        toks = m.get("clobTokenIds", [])
                        outs = m.get("outcomes", [])
                        if isinstance(toks, str):
                            toks = json.loads(toks)
                        if isinstance(outs, str):
                            outs = json.loads(outs)
                        return {o.lower(): t for o, t in zip(outs, toks)}
        except:
            pass
        return {}
    
    async def fetch_prices(self):
        if not self.tokens:
            return {}
        
        results = {}
        for side, token in self.tokens.items():
            try:
                async with self.session.get(
                    f"{CLOB_HOST}/midpoint",
                    params={"token_id": token},
                    timeout=aiohttp.ClientTimeout(total=2)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results[side] = float(data.get("mid", 0))
                    else:
                        results[side] = 0
            except:
                results[side] = 0
        
        return results
    
    async def get_outcome(self, slug):
        """Wait for outcome - up to 3 minutes"""
        print(f"\n  Waiting for {slug} to resolve...", flush=True)
        
        for attempt in range(90):  # 90 attempts x 2s = 3 minutes
            try:
                async with self.session.get(
                    f"{GAMMA_API}/markets",
                    params={"slug": slug},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        markets = await resp.json()
                        if markets:
                            m = markets[0]
                            
                            prices = m.get("outcomePrices", [])
                            outcomes = m.get("outcomes", [])
                            if isinstance(prices, str):
                                prices = json.loads(prices)
                            if isinstance(outcomes, str):
                                outcomes = json.loads(outcomes)
                            
                            # Check if resolved (prices are [1.0, 0.0] or [0.0, 1.0])
                            for o, p in zip(outcomes, prices):
                                if float(p) >= 0.99:
                                    winner = o.lower()
                                    print(f"  [{attempt*2}s] Resolved! Winner: {winner.upper()}")
                                    return winner
                            
                            # Not resolved yet
                            if attempt % 15 == 0:  # Print every 30s
                                print(f"\r  [{attempt*2}s] Waiting...  ", end="", flush=True)
            except:
                pass
            
            await asyncio.sleep(2)
        
        print(f"\n  Timeout after 180s - could not get outcome")
        return None
    
    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}")
    
    async def run(self, duration_hours=12):
        self.log("=" * 60)
        self.log("PAPER TRADING - LATE ENTRY 85-99c")
        self.log("=" * 60)
        self.log(f"Strategy: Buy {ENTRY_MIN*100:.0f}c-{ENTRY_MAX*100:.0f}c in LAST 3 MINUTES")
        self.log(f"Entry window: 3:00 to 0:30 (150 sec window)")
        self.log(f"Position size: {BALANCE_PCT*100:.0f}% per trade")
        self.log(f"Starting balance: ${self.balance:.2f} (paper money)")
        self.log("=" * 60)
        
        self.session = aiohttp.ClientSession()
        
        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        last_slug = None
        current_trade = None
        
        try:
            while time.time() < deadline:
                w = self.get_window()
                
                # New window?
                if w["slug"] != last_slug:
                    # Settle previous trade
                    if current_trade:
                        self.log(f"\nWaiting for outcome of {current_trade['slug']}...")
                        outcome = await self.get_outcome(current_trade['slug'])
                        
                        if outcome:
                            won = (outcome == current_trade['side'])
                            
                            if won:
                                payout = current_trade['shares']
                                profit = payout - current_trade['cost']
                                self.balance += profit
                                self.wins += 1
                                self.log(f"  WIN! {outcome.upper()} won | Profit: +${profit:.2f} | Balance: ${self.balance:.2f}")
                            else:
                                loss = current_trade['cost']
                                self.balance -= loss
                                self.losses += 1
                                self.log(f"  LOSS! {outcome.upper()} won | Lost: -${loss:.2f} | Balance: ${self.balance:.2f}")
                            
                            current_trade['outcome'] = outcome
                            current_trade['won'] = won
                            self.paper_trades.append(current_trade)
                        
                        current_trade = None
                    
                    # Setup new window
                    self.log(f"\nNEW WINDOW: {w['slug']}")
                    self.tokens = await self.fetch_tokens(w['slug'])
                    last_slug = w["slug"]
                    self.current_slug = w["slug"]
                
                # Get prices
                if self.tokens and not current_trade:
                    prices = await self.fetch_prices()
                    up = prices.get("up", 0)
                    down = prices.get("down", 0)
                    secs = w["secs_left"]
                    
                    # Entry logic: 30s <= secs_left <= 180s
                    if 30 <= secs <= ENTRY_WINDOW_SECS:
                        
                        # Check UP
                        if ENTRY_MIN <= up <= ENTRY_MAX and not current_trade:
                            use = self.balance * BALANCE_PCT
                            shares = use / up
                            
                            if shares >= MIN_SHARES:
                                current_trade = {
                                    'slug': w['slug'],
                                    'side': 'up',
                                    'price': up,
                                    'cost': use,
                                    'shares': shares,
                                    'entry_time': secs
                                }
                                self.log(f"  [PAPER] BUY UP @ {up*100:.1f}c [T-{secs}s]")
                                self.log(f"    Cost: ${use:.2f} | Shares: {shares:.1f}")
                        
                        # Check DOWN
                        elif ENTRY_MIN <= down <= ENTRY_MAX and not current_trade:
                            use = self.balance * BALANCE_PCT
                            shares = use / down
                            
                            if shares >= MIN_SHARES:
                                current_trade = {
                                    'slug': w['slug'],
                                    'side': 'down',
                                    'price': down,
                                    'cost': use,
                                    'shares': shares,
                                    'entry_time': secs
                                }
                                self.log(f"  [PAPER] BUY DOWN @ {down*100:.1f}c [T-{secs}s]")
                                self.log(f"    Cost: ${use:.2f} | Shares: {shares:.1f}")
                    
                    # Status
                    status = "HOLD" if current_trade else "SCAN"
                    print(f"\r  [{w['time_str']}] UP={up*100:.0f}c DOWN={down*100:.0f}c | {status} | W{self.wins}/L{self.losses} | ${self.balance:.2f}  ", end="", flush=True)
                
                await asyncio.sleep(1)
        
        except KeyboardInterrupt:
            self.log("\n\nStopped")
        finally:
            await self.session.close()
            self.print_summary()
    
    def print_summary(self):
        total = self.wins + self.losses
        win_rate = (self.wins / total * 100) if total > 0 else 0
        pnl = self.balance - self.starting_balance
        roi = (pnl / self.starting_balance * 100) if self.starting_balance > 0 else 0
        
        self.log("\n" + "=" * 60)
        self.log("PAPER TRADING RESULTS")
        self.log("=" * 60)
        self.log(f"Starting: ${self.starting_balance:.2f}")
        self.log(f"Final:    ${self.balance:.2f}")
        self.log(f"P&L:      ${pnl:+.2f}")
        self.log(f"Trades:   {total}")
        self.log(f"Wins:     {self.wins}")
        self.log(f"Losses:   {self.losses}")
        self.log(f"Win Rate: {win_rate:.1f}%")
        self.log(f"ROI:      {roi:+.1f}%")
        self.log("=" * 60)
        
        # Save results
        with open("pm_paper_results.json", "w") as f:
            json.dump({
                "config": {
                    "entry_min": ENTRY_MIN,
                    "entry_max": ENTRY_MAX,
                    "entry_window_secs": ENTRY_WINDOW_SECS,
                    "position_pct": BALANCE_PCT
                },
                "results": {
                    "starting": self.starting_balance,
                    "final": self.balance,
                    "pnl": pnl,
                    "roi": roi,
                    "trades": total,
                    "wins": self.wins,
                    "losses": self.losses,
                    "win_rate": win_rate
                },
                "trades": self.paper_trades
            }, f, indent=2)
        
        self.log(f"Results saved to pm_paper_results.json")


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=12)
    p.add_argument("--paper", action="store_true", help="Paper trade mode (no real orders)")
    args = p.parse_args()
    
    if args.paper:
        bot = PaperBot()
        asyncio.run(bot.run(duration_hours=args.duration))
    else:
        bot = FastBot()
        asyncio.run(bot.run(duration_hours=args.duration))


if __name__ == "__main__":
    main()

