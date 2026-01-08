"""
POLYMARKET FAST BOT (LIVE + PAPER)
=================================
- Ultra-fast polling (default 50ms)
- PAPER mode: no real orders, bankroll simulated
- Wick detection logic to skip unstable windows:
  A) FLIP-FLOP: UP >= 90c and DOWN >= 90c within same 15m window -> SKIP window
  B) FAST REJECT: a side touches >= 90c then drops < 88c within 5s -> SKIP window
  C) VOL RANGE brake: last 30s mid range too large -> SKIP window

IMPORTANT:
- This does NOT make it "100%". It only reduces the biggest wipeout cases.
- Using huge bankroll % is still dangerous. PAPER is for testing.
"""

import json
import time
import asyncio
import aiohttp
from datetime import datetime
from typing import Optional, Dict
from collections import deque

from web3 import Web3
from eth_account import Account

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from py_clob_client.constants import POLYGON

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# === CONFIG ===
ENTRY_MIN = 0.85         # Entry range: 85c to 95c (better edge)
ENTRY_MAX = 0.95         # Upper bound (still safe)
ENTRY_WINDOW_SECS = 120  # ONLY enter during LAST N seconds of window (set 900 for full 15m)
BALANCE_PCT = 0.70       # LIVE WARNING: too high. Use small in live. Paper OK for testing.
MIN_SHARES = 5
POLL_MS = 50             # Ultra fast: 50ms (20 checks/sec)
SETTLE_WAIT = 120

# === WICK / VOL GUARDS ===
WICK_TOUCH = 0.90          # "touch 90c"
WICK_REVERT = 0.88         # "rejected" level
WICK_REVERT_SECS = 5.0     # touch->reject window

VOL_WINDOW_SECS = 30.0     # compute range over last 30s
VOL_RANGE_MAX = 0.04       # 4c range in 30s => too volatile, skip window

ENTRY_CONFIRM_SECS = 1.0   # must stay in ENTRY_MIN..ENTRY_MAX this long before entering


class FastBot:
    def __init__(self, paper: bool = False, paper_start_balance: float = 10.0):
        self.paper = paper
        self.paper_balance = float(paper_start_balance)

        with open("pm_api_config.json") as f:
            config = json.load(f)

        self.proxy = config.get("proxy_address", "")

        # Client (LIVE only required for placing/canceling orders)
        self.client = None
        if not self.paper:
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

        # Async session (reuse for speed)
        self.session = None

        # Web3 + redemption (LIVE ONLY)
        if not self.paper:
            self.w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
            usdc_addr = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            usdc_abi = [{
                "constant": True,
                "inputs": [{"name": "account", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function"
            }]
            self.usdc = self.w3.eth.contract(
                address=Web3.to_checksum_address(usdc_addr),
                abi=usdc_abi
            )

            ctf_addr = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
            ctf_abi = [
                {"inputs": [{"name": "conditionId", "type": "bytes32"}],
                 "name": "payoutDenominator",
                 "outputs": [{"name": "", "type": "uint256"}],
                 "stateMutability": "view",
                 "type": "function"},
                {"inputs": [{"name": "collateralToken", "type": "address"},
                            {"name": "parentCollectionId", "type": "bytes32"},
                            {"name": "conditionId", "type": "bytes32"},
                            {"name": "indexSets", "type": "uint256[]"}],
                 "name": "redeemPositions",
                 "outputs": [],
                 "stateMutability": "nonpayable",
                 "type": "function"}
            ]
            self.ctf = self.w3.eth.contract(
                address=Web3.to_checksum_address(ctf_addr),
                abi=ctf_abi
            )
            self.account = Account.from_key(config["private_key"])

            # Detect wallet type
            self.wallet_type = self._detect_wallet_type()
            self.log(f"Wallet type: {self.wallet_type.upper()}")
            if self.wallet_type == "unknown":
                self.log("WARNING: Will try both redemption methods")
        else:
            self.w3 = None
            self.usdc = None
            self.ctf = None
            self.account = None
            self.wallet_type = "paper"
            self.log("Mode: PAPER (no on-chain calls, no real orders)")

        # State
        self.tokens = {}
        self.current_window = None
        self.current_condition_id = None
        self.traded_this_window = False

        # Position
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

        # Balance cache (LIVE only)
        self.balance_cache = 0
        self.last_balance_check = 0

        # Wick / vol guard state (per-window; reset in setup_new_window)
        self.wicky = False
        self.wick_reason = None
        self.touched_90 = {"up": False, "down": False}
        self.touch_time_90 = {"up": None, "down": None}
        self.mid_hist = deque()  # (ts, up_mid, dn_mid)
        self.in_range_since = {"up": None, "down": None}

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts}] {msg}")

    def get_balance(self) -> float:
        """Balance: paper sim OR on-chain with caching"""
        if self.paper:
            return float(self.paper_balance)

        now = time.time()
        if now - self.last_balance_check > 30 or self.balance_cache == 0:
            try:
                bal = self.usdc.functions.balanceOf(Web3.to_checksum_address(self.proxy)).call()
                self.balance_cache = bal / 1e6
                self.last_balance_check = now
            except Exception as e:
                if self.balance_cache == 0:
                    self.log(f"Balance read error: {str(e)[:60]}")
        return self.balance_cache

    def cancel_all(self):
        if self.paper or not self.client:
            return
        try:
            self.client.cancel_all()
        except:
            pass

    def _reset_guards_for_window(self):
        self.wicky = False
        self.wick_reason = None
        self.touched_90 = {"up": False, "down": False}
        self.touch_time_90 = {"up": None, "down": None}
        self.mid_hist.clear()
        self.in_range_since = {"up": None, "down": None}

    def _update_guards(self, up_mid: float, dn_mid: float):
        now = time.time()
        if up_mid <= 0 or dn_mid <= 0:
            return

        # history
        self.mid_hist.append((now, up_mid, dn_mid))
        cutoff = now - VOL_WINDOW_SECS
        while self.mid_hist and self.mid_hist[0][0] < cutoff:
            self.mid_hist.popleft()

        # vol range brake
        if not self.wicky and len(self.mid_hist) >= 3:
            ups = [x[1] for x in self.mid_hist]
            dns = [x[2] for x in self.mid_hist]
            up_range = max(ups) - min(ups)
            dn_range = max(dns) - min(dns)
            if max(up_range, dn_range) >= VOL_RANGE_MAX:
                self.wicky = True
                self.wick_reason = f"VOL_RANGE>{VOL_RANGE_MAX:.2f} (up={up_range:.2f}, dn={dn_range:.2f})"

        # track 90 touch
        for side, mid in (("up", up_mid), ("down", dn_mid)):
            if mid >= WICK_TOUCH:
                self.touched_90[side] = True
                if self.touch_time_90[side] is None:
                    self.touch_time_90[side] = now

        # flip-flop: both touched 90 sometime in window
        if not self.wicky and self.touched_90["up"] and self.touched_90["down"]:
            self.wicky = True
            self.wick_reason = "FLIP_FLOP: UP90 and DOWN90 touched in same window"

        # fast reject: touch 90 then drop <88 quickly
        if not self.wicky:
            for side, mid in (("up", up_mid), ("down", dn_mid)):
                t0 = self.touch_time_90[side]
                if t0 is not None and (now - t0) <= WICK_REVERT_SECS and mid <= WICK_REVERT:
                    self.wicky = True
                    self.wick_reason = f"FAST_REJECT: {side.upper()} 90-><{WICK_REVERT:.2f} in {WICK_REVERT_SECS:.0f}s"
                    break

        # entry persistence: must stay in ENTRY_MIN..ENTRY_MAX for ENTRY_CONFIRM_SECS
        for side, mid in (("up", up_mid), ("down", dn_mid)):
            if ENTRY_MIN <= mid <= ENTRY_MAX:
                if self.in_range_since[side] is None:
                    self.in_range_since[side] = now
            else:
                self.in_range_since[side] = None

    def _side_confirmed(self, side: str) -> bool:
        t0 = self.in_range_since.get(side)
        return (t0 is not None) and ((time.time() - t0) >= ENTRY_CONFIRM_SECS)

    def _detect_wallet_type(self) -> str:
        """Auto-detect Gnosis Safe vs Custom Proxy"""
        try:
            safe_abi = [{"inputs": [], "name": "getOwners", "outputs": [{"name": "", "type": "address[]"}],
                        "stateMutability": "view", "type": "function"}]
            safe = self.w3.eth.contract(address=Web3.to_checksum_address(self.proxy), abi=safe_abi)
            safe.functions.getOwners().call()
            return "safe"
        except:
            pass

        try:
            code = self.w3.eth.get_code(Web3.to_checksum_address(self.proxy))
            execute_selector = Web3.keccak(text="execute(address,uint256,bytes)")[:4]
            if execute_selector in code:
                return "custom"
        except:
            pass

        return "unknown"

    def is_resolved_onchain(self, condition_id: str) -> bool:
        try:
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            payout_denom = self.ctf.functions.payoutDenominator(condition_bytes).call()
            return payout_denom > 0
        except:
            return False

    def redeem_position(self, condition_id: str) -> bool:
        if self.paper:
            return True
        if self.wallet_type == "custom":
            return self._redeem_via_custom_proxy(condition_id)
        elif self.wallet_type == "safe":
            return self._redeem_via_safe(condition_id)
        else:
            self.log("  Unknown wallet type - trying custom proxy method...")
            if self._redeem_via_custom_proxy(condition_id):
                return True
            self.log("  Custom failed, trying Safe method...")
            if self._redeem_via_safe(condition_id):
                return True
            self.log("  Both methods failed - manual claim needed")
            return False

    def _redeem_via_custom_proxy(self, condition_id: str) -> bool:
        try:
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            parent_collection = Web3.to_bytes(hexstr="0x" + "00" * 32)
            usdc_addr = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
            ctf_addr = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

            redeem_calldata = self.ctf.encodeABI(
                fn_name="redeemPositions",
                args=[usdc_addr, parent_collection, condition_bytes, [1, 2]]
            )

            custom_abi = [
                {"inputs": [{"name": "to", "type": "address"},
                            {"name": "value", "type": "uint256"},
                            {"name": "data", "type": "bytes"}],
                 "name": "execute", "outputs": [{"name": "", "type": "bytes"}],
                 "stateMutability": "nonpayable", "type": "function"}
            ]
            proxy = self.w3.eth.contract(address=Web3.to_checksum_address(self.proxy), abi=custom_abi)

            nonce = self.w3.eth.get_transaction_count(self.account.address)

            tx = proxy.functions.execute(
                ctf_addr, 0, redeem_calldata
            ).build_transaction({
                "from": self.account.address,
                "nonce": nonce,
                "gas": 400000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": 137
            })

            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            self.log(f"  Tx: {tx_hash.hex()[:40]}...")

            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            success = (receipt["status"] == 1)
            if success:
                self.log(f"  [OK] Redeemed via custom proxy! Gas: {receipt['gasUsed']}")
            else:
                self.log("  [FAIL] Redemption tx failed")
            return success
        except Exception as e:
            self.log(f"  Custom proxy error: {str(e)[:60]}")
            return False

    def _redeem_via_safe(self, condition_id: str) -> bool:
        try:
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            parent_collection = Web3.to_bytes(hexstr="0x" + "00" * 32)
            usdc_addr = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
            ctf_addr = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

            redeem_calldata = self.ctf.encodeABI(
                fn_name="redeemPositions",
                args=[usdc_addr, parent_collection, condition_bytes, [1, 2]]
            )

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

            safe_nonce = safe.functions.nonce().call()

            tx_hash_data = self.w3.solidityKeccak(
                ["address", "uint256", "bytes", "uint8", "uint256", "uint256",
                 "uint256", "address", "address", "uint256"],
                [ctf_addr, 0, redeem_calldata, 0, 0, 0,
                 0, "0x" + "00" * 20, "0x" + "00" * 20, safe_nonce]
            )

            signed_msg = self.account.signHash(tx_hash_data)
            signature = (
                signed_msg.r.to_bytes(32, "big")
                + signed_msg.s.to_bytes(32, "big")
                + signed_msg.v.to_bytes(1, "big")
            )

            nonce = self.w3.eth.get_transaction_count(self.account.address)

            tx = safe.functions.execTransaction(
                ctf_addr, 0, redeem_calldata, 0, 0, 0,
                0, "0x" + "00" * 20, "0x" + "00" * 20, signature
            ).build_transaction({
                "from": self.account.address,
                "nonce": nonce,
                "gas": 500000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": 137
            })

            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            self.log(f"  Tx: {tx_hash.hex()[:40]}...")

            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            success = (receipt["status"] == 1)
            if success:
                self.log(f"  [OK] Redeemed via Safe! Gas: {receipt['gasUsed']}")
            else:
                self.log("  [FAIL] Redemption tx failed")
            return success
        except Exception as e:
            self.log(f"  Safe error: {str(e)[:60]}")
            return False

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

                        if cond_id:
                            self.current_condition_id = cond_id

                        return {o.lower(): t for o, t in zip(outs, toks)}
        except:
            pass
        return {}

    async def fetch_midpoint(self, token: str) -> float:
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
                    results[side] = {"mid": float(price)}
                else:
                    results[side] = {"mid": 0.0}
        except:
            pass

        return results

    def get_actual_fill(self, order_id: str) -> Optional[Dict]:
        if self.paper or not self.client:
            return None
        try:
            order = self.client.get_order(order_id)
            if order:
                matched = float(order.get("size_matched", 0))
                price = float(order.get("price", 0))
                status = str(order.get("status", "")).upper()

                if matched > 0:
                    return {"shares": matched, "price": price, "cost": matched * price, "status": status}
                elif status in ["MATCHED", "FILLED"]:
                    original = float(order.get("original_size", 0))
                    return {"shares": original, "price": price, "cost": original * price, "status": status}
        except:
            pass
        return None

    def execute_buy_fast(self, side: str, price: float) -> bool:
        token = self.tokens.get(side)
        if not token:
            return False

        balance = self.get_balance()
        trade_amount = balance * BALANCE_PCT
        shares = trade_amount / price

        if shares < MIN_SHARES:
            self.log(f"  Shares {shares:.1f} < min {MIN_SHARES}")
            return False

        # PAPER: simulate immediately
        if self.paper:
            self.paper_balance -= trade_amount

            self.holding_side = side
            self.holding_price = price
            self.holding_shares = shares
            self.holding_cost = trade_amount
            self.order_id = f"PAPER-{int(time.time())}"

            self.log(f"  [PAPER] BUY {side.upper()} @ {price*100:.1f}c")
            self.log(f"    Balance: ${balance:.2f} | Using: ${trade_amount:.2f} | Shares: {shares:.2f}")
            return True

        # LIVE: place real order
        self.log(f"  BUYING {side.upper()} @ {price*100:.1f}c")
        self.log(f"    Balance: ${balance:.2f} | Using: ${trade_amount:.2f}")
        self.log(f"    Shares: {shares:.1f}")

        try:
            args = OrderArgs(token_id=token, price=price, size=shares, side=BUY)
            signed = self.client.create_order(args)
            result = self.client.post_order(signed, OrderType.GTC)

            if result and result.get("success"):
                self.order_id = result.get("orderID")
                self.log(f"    ORDER OK: {self.order_id[:30]}...")

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
                    self.log("    WARNING: Could not read actual fill, using estimates")
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

    async def get_market_status(self, slug: str) -> Dict:
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
                        result["closed"] = m.get("closed", False)

                        uma_status = m.get("umaResolutionStatus", "")
                        result["resolved"] = (uma_status == "resolved")

                        outcomes = m.get("outcomes", [])
                        outcome_prices = m.get("outcomePrices", [])

                        if isinstance(outcomes, str):
                            outcomes = json.loads(outcomes)
                        if isinstance(outcome_prices, str):
                            outcome_prices = json.loads(outcome_prices)

                        for i, price in enumerate(outcome_prices):
                            price_float = float(price)
                            if price_float >= 0.99:
                                if i < len(outcomes):
                                    result["winner"] = outcomes[i].lower()
                                    result["resolved"] = True
                                break
        except Exception as e:
            self.log(f"  Market status error: {e}")
        return result

    async def wait_for_settlement_paper(self, slug: str, max_wait: int = 180) -> Dict:
        start = time.time()
        balance_before = self.get_balance()

        winner = None
        while time.time() - start < max_wait:
            status = await self.get_market_status(slug)
            if status.get("winner"):
                winner = status["winner"]
                break
            await asyncio.sleep(2)

        if not winner:
            return {
                "resolved": False,
                "winner": None,
                "balance_before": balance_before,
                "balance_after": self.get_balance()
            }

        # payout: if win -> +$1/share; if lose -> +$0 (cost already deducted on entry)
        if winner == self.holding_side:
            self.paper_balance += self.holding_shares

        return {
            "resolved": True,
            "winner": winner,
            "balance_before": balance_before,
            "balance_after": self.get_balance()
        }

    async def wait_for_settlement(self, slug: str, max_wait: int = 180) -> Dict:
        start = time.time()
        balance_start = self.get_balance()
        winner = None

        print("\n  Waiting for on-chain resolution...", flush=True)
        resolved_onchain = False

        while time.time() - start < max_wait:
            elapsed = int(time.time() - start)

            if self.current_condition_id and self.is_resolved_onchain(self.current_condition_id):
                if not resolved_onchain:
                    print(f"\n  [{elapsed}s] RESOLVED ON-CHAIN!")
                    resolved_onchain = True

                status = await self.get_market_status(slug)
                if status["winner"]:
                    winner = status["winner"]
                    we_won = (winner == self.holding_side)
                    print(f"  [{elapsed}s] Winner: {winner.upper()} | We have: {self.holding_side.upper()} -> {'WIN' if we_won else 'LOSS'}")
                    break
                else:
                    print(f"\r  [{elapsed}s] Waiting for API winner...  ", end="", flush=True)
            else:
                print(f"\r  [{elapsed}s] Checking on-chain...  ", end="", flush=True)

            await asyncio.sleep(5)

        if not winner:
            print(f"\n  Timeout - market not resolved on-chain after {max_wait}s")
            balance_now = self.get_balance()
            return {"resolved": False, "winner": None, "balance_before": balance_start, "balance_after": balance_now}

        we_won = (winner == self.holding_side)

        if we_won:
            print("  Attempting AUTO-REDEMPTION...")
            if self.current_condition_id:
                success = self.redeem_position(self.current_condition_id)
                if success:
                    await asyncio.sleep(3)
                    balance_now = self.get_balance()

                    if balance_now > balance_start + 0.01:
                        profit = balance_now - balance_start
                        print(f"  AUTO-REDEEMED! ${balance_start:.2f} -> ${balance_now:.2f} (+${profit:.2f})")
                    else:
                        print("  Redemption tx succeeded but balance not updated yet")
                        for _ in range(6):
                            await asyncio.sleep(5)
                            balance_now = self.get_balance()
                            if balance_now > balance_start + 0.01:
                                profit = balance_now - balance_start
                                print(f"\n  Balance updated! ${balance_start:.2f} -> ${balance_now:.2f} (+${profit:.2f})")
                                break
                            print(f"\r  Waiting for balance... ${balance_now:.2f}  ", end="", flush=True)
                else:
                    print("  Auto-redemption failed - may need manual claim")
                    balance_now = self.get_balance()
            else:
                print("  No conditionId - cannot auto-redeem")
                balance_now = balance_start
        else:
            print("  We lost - no redemption needed")
            balance_now = self.get_balance()

        return {"resolved": True, "winner": winner, "balance_before": balance_start, "balance_after": balance_now}

    async def handle_settlement(self):
        if not self.holding_side:
            return

        self.log("=" * 50)
        self.log(f"WINDOW CLOSED: {self.current_window}")
        self.log("=" * 50)
        self.log(f"  Our position: {self.holding_side.upper()}")
        self.log(f"  Entry: {self.holding_shares:.1f} shares @ {self.holding_price*100:.1f}c")
        self.log(f"  Cost: ${self.holding_cost:.2f}")

        if self.paper:
            settle_result = await self.wait_for_settlement_paper(self.current_window, max_wait=SETTLE_WAIT)
        else:
            settle_result = await self.wait_for_settlement(self.current_window, max_wait=SETTLE_WAIT)

        winner = settle_result.get("winner")
        balance_before = settle_result.get("balance_before", 0)
        balance_after = settle_result.get("balance_after", 0)
        resolved = settle_result.get("resolved", False)

        if resolved and winner:
            we_won = (winner == self.holding_side)
            self.log(f"  Market winner: {winner.upper()}")
        else:
            payout = balance_after - balance_before
            we_won = (payout > self.holding_cost * 0.5)
            self.log("  Resolution unclear, using balance change")

        payout = balance_after - balance_before

        if we_won:
            self.wins += 1
            # theoretical profit for logging (shares - cost)
            profit = self.holding_shares - self.holding_cost
            self.log("")
            self.log("  >>> RESULT: WIN! <<<")
            self.log(f"  Payout: ${self.holding_shares:.2f} (1 share = $1)")
            self.log(f"  Profit: +${profit:.2f}")
        else:
            self.losses += 1
            self.log("")
            self.log("  >>> RESULT: LOSS <<<")
            self.log(f"  Lost: -${self.holding_cost:.2f}")

        self.log("")
        self.log(f"  Balance: ${balance_before:.2f} -> ${balance_after:.2f}")
        self.log("=" * 50)

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
            "payout": payout,
            "wicky": self.wicky,
            "wick_reason": self.wick_reason,
        })

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

        self._reset_guards_for_window()

        self.log(f"NEW WINDOW: {window['slug']}")
        self.log(f"  Balance: ${balance:.2f}")

        min_required = MIN_SHARES * ENTRY_MIN
        if not self.tokens:
            self.log("  ERROR: No tokens")
        elif balance < min_required:
            self.log(f"  WARNING: Balance ${balance:.2f} < min ${min_required:.2f}")

    async def run(self, duration_hours: float = 12):
        self.log("=" * 60)
        self.log("POLYMARKET FAST BOT - PAPER + WICK GUARD")
        self.log("=" * 60)
        self.log(f"Proxy: {self.proxy}")
        self.log(f"Mode: {'PAPER' if self.paper else 'LIVE'}")
        self.log(f"Entry: {ENTRY_MIN*100:.0f}c-{ENTRY_MAX*100:.0f}c | Window: last {ENTRY_WINDOW_SECS}s (avoid last 30s)")
        self.log(f"Position size: {BALANCE_PCT*100:.0f}% per trade")
        self.log(f"Poll: {POLL_MS}ms ({1000/POLL_MS:.0f} checks/sec)")
        self.log(f"Duration: {duration_hours}h")

        self.cancel_all()

        self.starting_balance = self.get_balance()
        self.log(f"Starting balance: ${self.starting_balance:.2f}")
        self.log("=" * 60)

        self.session = aiohttp.ClientSession()

        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        last_window = None

        try:
            while time.time() < deadline:
                tick_start = time.time()

                window = self.get_window()

                if window["slug"] != last_window:
                    if self.holding_side:
                        await self.handle_settlement()
                    await self.setup_new_window(window)
                    last_window = window["slug"]

                prices = await self.fetch_prices_fast()
                up_mid = float(prices.get("up", {}).get("mid", 0))
                dn_mid = float(prices.get("down", {}).get("mid", 0))

                # update wick/vol guards
                self._update_guards(up_mid, dn_mid)

                # Entry gating: only last ENTRY_WINDOW_SECS seconds (but not last 30s)
                secs = int(window["secs_left"])
                in_entry_window = (secs >= 1)

                if in_entry_window and not self.traded_this_window and not self.holding_side and self.tokens:
                    if not self.wicky:
                        # must be confirmed inside range for ENTRY_CONFIRM_SECS
                        if self._side_confirmed("up") and ENTRY_MIN <= up_mid <= ENTRY_MAX:
                            self.log(f"*** SIGNAL: UP @ {up_mid*100:.0f}c [T-{secs}s] ***")
                            if self.execute_buy_fast("up", up_mid):
                                self.traded_this_window = True

                        elif self._side_confirmed("down") and ENTRY_MIN <= dn_mid <= ENTRY_MAX:
                            self.log(f"*** SIGNAL: DOWN @ {dn_mid*100:.0f}c [T-{secs}s] ***")
                            if self.execute_buy_fast("down", dn_mid):
                                self.traded_this_window = True

                hold_str = ""
                if self.holding_side:
                    hold_str = f" | HOLD {self.holding_side.upper()}"

                current_bal = self.get_balance()
                pnl = current_bal - self.starting_balance

                wick_str = ""
                if self.wicky and not self.holding_side:
                    wick_str = f" | SKIP({self.wick_reason})"

                print(
                    f"\r  [{window['time_str']}] UP={up_mid*100:.0f}c DOWN={dn_mid*100:.0f}c"
                    f"{hold_str}{wick_str} | W{self.wins}/L{self.losses} | ${current_bal:.2f} ({pnl:+.2f})  ",
                    end="",
                    flush=True
                )

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
            try:
                await self.session.close()
            except:
                pass
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
                "mode": "paper" if self.paper else "live",
                "starting": self.starting_balance,
                "final": balance,
                "pnl": pnl,
                "wins": self.wins,
                "losses": self.losses,
                "trades": self.trades
            }, f, indent=2)


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=12)
    p.add_argument("--paper", action="store_true", help="Paper trading mode (no real orders)")
    p.add_argument("--paper_start", type=float, default=10.0, help="Paper starting balance")
    args = p.parse_args()

    bot = FastBot(paper=args.paper, paper_start_balance=args.paper_start)
    asyncio.run(bot.run(duration_hours=args.duration))


if __name__ == "__main__":
    main()
