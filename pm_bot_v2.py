"""
POLYMARKET BOT v2 - WITH RETRY LOGIC
=====================================
Fixes:
1. Retry failed orders up to 3 times
2. Better error handling
3. Continues running properly
"""

import json
import time
import requests
from datetime import datetime
from typing import Optional, Dict
from web3 import Web3

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from py_clob_client.constants import POLYGON

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# Config
ENTRY_PRICE = 0.90
BALANCE_PCT = 0.90
MIN_SHARES = 5
ORDER_DELAY = 5         # Longer delay between orders
RETRY_DELAY = 3         # Delay between retries
MAX_RETRIES = 3         # Retry failed orders
POLL_INTERVAL = 5
SETTLE_WAIT = 120
MIN_TIME_TO_TRADE = 60


class PolymarketBotV2:
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
        
        self.w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        usdc_addr = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        usdc_abi = [{"constant":True,"inputs":[{"name":"account","type":"address"}],
                     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
        self.usdc = self.w3.eth.contract(address=Web3.to_checksum_address(usdc_addr), abi=usdc_abi)
        
        self.session = requests.Session()
        
        # State
        self.current_window = None
        self.tokens = {}
        self.orders = {}
        self.filled_side = None
        self.filled_cost = 0
        self.traded_this_window = False
        
        # Stats
        self.trades = []
        self.wins = 0
        self.losses = 0
    
    def get_balance(self) -> float:
        try:
            bal = self.usdc.functions.balanceOf(Web3.to_checksum_address(self.proxy)).call()
            return bal / 1e6
        except Exception as e:
            self.log(f"Balance error: {e}")
            return 0
    
    def cancel_all(self):
        try:
            self.client.cancel_all()
        except:
            pass
        for side, oid in list(self.orders.items()):
            try:
                self.client.cancel(oid)
            except:
                pass
        self.orders = {}
    
    def get_window(self) -> Dict:
        ts = int(time.time())
        start = ts - (ts % 900)
        end = start + 900
        return {
            "slug": f"btc-updown-15m-{start}",
            "start": start,
            "end": end,
            "secs_left": end - ts,
            "time_str": f"{(end-ts)//60}:{(end-ts)%60:02d}"
        }
    
    def get_tokens(self, slug: str) -> Dict:
        try:
            r = self.session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
            if r.status_code == 200:
                markets = r.json()
                if markets:
                    m = markets[0]
                    toks = m.get("clobTokenIds", [])
                    outs = m.get("outcomes", [])
                    if isinstance(toks, str):
                        toks = json.loads(toks)
                    if isinstance(outs, str):
                        outs = json.loads(outs)
                    return {o.lower(): t for o, t in zip(outs, toks)}
        except Exception as e:
            self.log(f"Token error: {e}")
        return {}
    
    def get_prices(self) -> Dict:
        prices = {}
        for side, token in self.tokens.items():
            try:
                r = self.session.get(f"{CLOB_HOST}/midpoint", params={"token_id": token}, timeout=5)
                prices[side] = float(r.json().get("mid", 0))
            except:
                prices[side] = 0
        return prices
    
    def place_order_with_retry(self, side: str, amount: float) -> Optional[str]:
        """Place order with retry logic"""
        token = self.tokens.get(side)
        if not token:
            return None
        
        shares = amount / ENTRY_PRICE
        if shares < MIN_SHARES:
            self.log(f"  [{side.upper()}] {shares:.1f} < min {MIN_SHARES}, skipping")
            return None
        
        for attempt in range(1, MAX_RETRIES + 1):
            self.log(f"  [{side.upper()}] Attempt {attempt}: {shares:.1f} shares @ {ENTRY_PRICE*100:.0f}c")
            
            try:
                args = OrderArgs(token_id=token, price=ENTRY_PRICE, size=shares, side=BUY)
                signed = self.client.create_order(args)
                result = self.client.post_order(signed, OrderType.GTC)
                
                if result and result.get("success"):
                    oid = result.get("orderID")
                    self.log(f"  [{side.upper()}] OK: {oid[:30]}...")
                    return oid
                else:
                    self.log(f"  [{side.upper()}] Failed: {result}")
                    
            except Exception as e:
                err = str(e)
                if "not enough balance" in err.lower():
                    self.log(f"  [{side.upper()}] Insufficient balance - stopping retries")
                    return None
                elif "500" in err or "could not run" in err.lower():
                    self.log(f"  [{side.upper()}] Server error (500) - retrying...")
                else:
                    self.log(f"  [{side.upper()}] Error: {err[:60]}")
            
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
        
        self.log(f"  [{side.upper()}] All {MAX_RETRIES} attempts failed")
        return None
    
    def is_filled(self, order_id: str) -> bool:
        try:
            order = self.client.get_order(order_id)
            if order:
                status = str(order.get("status", "")).upper()
                if status in ["MATCHED", "FILLED", "CLOSED"]:
                    return True
                matched = float(order.get("size_matched", 0))
                original = float(order.get("original_size", 1))
                if matched >= original * 0.5:
                    return True
        except:
            pass
        return False
    
    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}")
    
    def on_new_window(self, window: Dict):
        """Handle new window"""
        
        # Settlement
        if self.filled_side:
            self.log(f"SETTLEMENT: Waiting {SETTLE_WAIT}s for {self.filled_side.upper()}...")
            
            balance_before = self.get_balance()
            time.sleep(SETTLE_WAIT)
            balance_after = self.get_balance()
            
            change = balance_after - balance_before
            
            if change > 0:
                self.wins += 1
                self.log(f"SETTLEMENT: WIN! +${change:.2f}")
            else:
                self.losses += 1
                self.log(f"SETTLEMENT: LOSS (change: ${change:.2f})")
            
            self.trades.append({
                "window": self.current_window,
                "side": self.filled_side,
                "cost": self.filled_cost,
                "change": change,
                "result": "WIN" if change > 0 else "LOSS"
            })
        
        # Cancel all
        self.cancel_all()
        
        # Reset state
        self.current_window = window["slug"]
        self.tokens = {}
        self.orders = {}
        self.filled_side = None
        self.filled_cost = 0
        self.traded_this_window = False
        
        # Get tokens
        self.tokens = self.get_tokens(self.current_window)
        if not self.tokens:
            self.log(f"NEW WINDOW: {window['slug']} - No tokens")
            return
        
        # Check balance
        balance = self.get_balance()
        min_required = MIN_SHARES * ENTRY_PRICE * 2
        
        self.log(f"NEW WINDOW: {window['slug']}")
        self.log(f"  Balance: ${balance:.2f} (need ${min_required:.2f})")
        
        if balance < min_required:
            self.log(f"  SKIP: Insufficient balance")
            return
        
        # Check time - don't trade too late in window
        if window["secs_left"] < MIN_TIME_TO_TRADE:
            self.log(f"  SKIP: Only {window['secs_left']}s left (need {MIN_TIME_TO_TRADE}s)")
            return
        
        # Place orders with retry
        usable = balance * BALANCE_PCT
        per_side = usable / 2
        
        self.log(f"  Trading: 90% of ${balance:.2f} = ${usable:.2f} -> ${per_side:.2f}/side")
        
        # Place UP order with retry
        up_oid = self.place_order_with_retry("up", per_side)
        if up_oid:
            self.orders["up"] = up_oid
        
        time.sleep(ORDER_DELAY)
        
        # Place DOWN order with retry
        down_oid = self.place_order_with_retry("down", per_side)
        if down_oid:
            self.orders["down"] = down_oid
        
        if self.orders:
            self.traded_this_window = True
            self.log(f"  Orders active: {list(self.orders.keys())}")
        else:
            self.log(f"  WARNING: No orders placed!")
    
    def check_fills(self):
        if self.filled_side or not self.orders:
            return
        
        for side, oid in list(self.orders.items()):
            if self.is_filled(oid):
                self.log(f"FILLED: {side.upper()}!")
                self.filled_side = side
                
                balance = self.get_balance()
                self.filled_cost = balance * BALANCE_PCT / 2
                
                # Cancel other side
                other = "down" if side == "up" else "up"
                if other in self.orders:
                    self.log(f"  Cancelling {other.upper()}...")
                    try:
                        self.client.cancel(self.orders[other])
                    except:
                        pass
                
                self.orders = {}
                break
    
    def run(self, duration_hours: float = 12):
        self.log("=" * 60)
        self.log("POLYMARKET BOT v2 - WITH RETRY")
        self.log("=" * 60)
        self.log(f"Proxy: {self.proxy}")
        self.log(f"Strategy: Buy at {ENTRY_PRICE*100:.0f}c, settle at $1")
        self.log(f"Retries: {MAX_RETRIES} per order")
        self.log(f"Duration: {duration_hours}h")
        
        self.cancel_all()
        
        balance = self.get_balance()
        starting_balance = balance
        self.log(f"Starting balance: ${balance:.2f}")
        self.log("=" * 60)
        
        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        last_window = None
        
        try:
            while time.time() < deadline:
                window = self.get_window()
                
                if window["slug"] != last_window:
                    self.on_new_window(window)
                    last_window = window["slug"]
                
                self.check_fills()
                
                # Status
                if self.tokens:
                    prices = self.get_prices()
                    up = prices.get("up", 0)
                    dn = prices.get("down", 0)
                    
                    if self.filled_side:
                        status = f"HOLD {self.filled_side.upper()}"
                    elif self.orders:
                        status = f"ORDERS: {list(self.orders.keys())}"
                    else:
                        status = "IDLE"
                    
                    balance = self.get_balance()
                    pnl = balance - starting_balance
                    
                    print(f"\r  [{window['time_str']}] Up:{up*100:.0f}c Dn:{dn*100:.0f}c | {status} | ${balance:.2f} ({pnl:+.2f})", end="", flush=True)
                
                time.sleep(POLL_INTERVAL)
        
        except KeyboardInterrupt:
            self.log("\n\nSTOPPED")
        except Exception as e:
            self.log(f"\n\nERROR: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.log("\nCleanup...")
            self.cancel_all()
            self.summary(starting_balance)
    
    def summary(self, starting_balance: float):
        balance = self.get_balance()
        pnl = balance - starting_balance
        
        self.log("=" * 60)
        self.log("SUMMARY")
        self.log("=" * 60)
        self.log(f"Starting: ${starting_balance:.2f}")
        self.log(f"Final:    ${balance:.2f}")
        self.log(f"P&L:      ${pnl:+.2f}")
        self.log(f"Wins:     {self.wins}")
        self.log(f"Losses:   {self.losses}")
        
        with open("pm_bot_results.json", "w") as f:
            json.dump({
                "starting": starting_balance,
                "final": balance,
                "pnl": pnl,
                "wins": self.wins,
                "losses": self.losses,
                "trades": self.trades
            }, f, indent=2)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=12, help="Hours")
    args = p.parse_args()
    
    bot = PolymarketBotV2()
    bot.run(duration_hours=args.duration)

