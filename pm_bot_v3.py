"""
POLYMARKET BOT v3 - WAIT FOR 90c
=================================
Strategy: 
- Watch prices
- When either side reaches >= 90c, buy that side
- Let it settle at $1

This is the strategy that works with paper trading.
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
ENTRY_THRESHOLD = 0.90  # Buy when price >= 90c
BALANCE_PCT = 0.45      # Use 45% per trade (single side)
MIN_SHARES = 5
POLL_INTERVAL = 3       # Check every 3 seconds
SETTLE_WAIT = 120
MIN_TIME_TO_TRADE = 60  # Don't trade in last minute


class PolymarketBotV3:
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
        self.holding = None
        self.holding_cost = 0
        self.traded_this_window = False
        
        # Stats
        self.trades = []
        self.wins = 0
        self.losses = 0
    
    def get_balance(self) -> float:
        try:
            bal = self.usdc.functions.balanceOf(Web3.to_checksum_address(self.proxy)).call()
            return bal / 1e6
        except:
            return 0
    
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
        except:
            pass
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
    
    def buy(self, side: str, price: float, amount: float) -> bool:
        """Buy at current market price"""
        token = self.tokens.get(side)
        if not token:
            return False
        
        shares = amount / price
        if shares < MIN_SHARES:
            self.log(f"  {shares:.1f} shares < min {MIN_SHARES}")
            return False
        
        self.log(f"  BUYING {side.upper()}: {shares:.1f} shares @ {price*100:.0f}c = ${amount:.2f}")
        
        try:
            args = OrderArgs(token_id=token, price=price, size=shares, side=BUY)
            signed = self.client.create_order(args)
            result = self.client.post_order(signed, OrderType.GTC)
            
            if result and result.get("success"):
                self.log(f"  ORDER PLACED: {result.get('orderID', '')[:30]}...")
                
                # Check if it filled immediately (market order style)
                time.sleep(2)
                try:
                    order = self.client.get_order(result["orderID"])
                    if order:
                        status = str(order.get("status", "")).upper()
                        if status in ["MATCHED", "FILLED"]:
                            self.log(f"  FILLED!")
                            return True
                        matched = float(order.get("size_matched", 0))
                        if matched > 0:
                            self.log(f"  PARTIALLY FILLED: {matched:.1f} shares")
                            return True
                except:
                    pass
                
                # Even if not immediately filled, count it as we have the order
                return True
            else:
                self.log(f"  ORDER FAILED: {result}")
                
        except Exception as e:
            err = str(e)
            if "not enough balance" in err.lower():
                self.log(f"  Insufficient balance")
            else:
                self.log(f"  ERROR: {err[:60]}")
        
        return False
    
    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}")
    
    def on_new_window(self, window: Dict):
        """Handle new window"""
        
        # Settlement from previous window
        if self.holding:
            self.log(f"SETTLEMENT: Waiting {SETTLE_WAIT}s for {self.holding.upper()}...")
            
            balance_before = self.get_balance()
            time.sleep(SETTLE_WAIT)
            balance_after = self.get_balance()
            
            change = balance_after - balance_before
            
            if change > 0:
                self.wins += 1
                self.log(f"SETTLEMENT: WIN! +${change:.2f}")
            else:
                self.losses += 1
                self.log(f"SETTLEMENT: LOSS")
            
            self.trades.append({
                "window": self.current_window,
                "side": self.holding,
                "cost": self.holding_cost,
                "change": change
            })
            
            self.holding = None
            self.holding_cost = 0
        
        # Cancel all
        self.cancel_all()
        
        # Reset
        self.current_window = window["slug"]
        self.tokens = self.get_tokens(self.current_window)
        self.traded_this_window = False
        
        self.log(f"NEW WINDOW: {window['slug']}")
        
        if not self.tokens:
            self.log("  No tokens found")
    
    def run(self, duration_hours: float = 12):
        self.log("=" * 60)
        self.log("POLYMARKET BOT v3 - WAIT FOR 90c")
        self.log("=" * 60)
        self.log(f"Proxy: {self.proxy}")
        self.log(f"Strategy: Buy when price >= {ENTRY_THRESHOLD*100:.0f}c")
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
                
                # New window?
                if window["slug"] != last_window:
                    self.on_new_window(window)
                    last_window = window["slug"]
                
                # Watch for entry opportunity
                if not self.traded_this_window and not self.holding and self.tokens:
                    prices = self.get_prices()
                    up = prices.get("up", 0)
                    dn = prices.get("down", 0)
                    
                    # Check time
                    if window["secs_left"] < MIN_TIME_TO_TRADE:
                        # Too late to trade
                        pass
                    elif up >= ENTRY_THRESHOLD:
                        # UP is at 90c+ - BUY IT!
                        self.log(f"*** UP at {up*100:.0f}c >= {ENTRY_THRESHOLD*100:.0f}c ***")
                        
                        balance = self.get_balance()
                        trade_amount = balance * BALANCE_PCT
                        
                        if self.buy("up", up, trade_amount):
                            self.holding = "up"
                            self.holding_cost = trade_amount
                            self.traded_this_window = True
                            
                    elif dn >= ENTRY_THRESHOLD:
                        # DOWN is at 90c+ - BUY IT!
                        self.log(f"*** DOWN at {dn*100:.0f}c >= {ENTRY_THRESHOLD*100:.0f}c ***")
                        
                        balance = self.get_balance()
                        trade_amount = balance * BALANCE_PCT
                        
                        if self.buy("down", dn, trade_amount):
                            self.holding = "down"
                            self.holding_cost = trade_amount
                            self.traded_this_window = True
                
                # Status
                if self.tokens:
                    prices = self.get_prices()
                    up = prices.get("up", 0)
                    dn = prices.get("down", 0)
                    
                    if self.holding:
                        status = f"HOLD {self.holding.upper()}"
                    else:
                        status = f"WAIT for >=90c"
                    
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
            self.cancel_all()
            self.summary(starting_balance)
    
    def summary(self, starting_balance: float):
        balance = self.get_balance()
        pnl = balance - starting_balance
        
        self.log("\n" + "=" * 60)
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
    
    bot = PolymarketBotV3()
    bot.run(duration_hours=args.duration)

