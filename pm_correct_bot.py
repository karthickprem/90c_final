"""
POLYMARKET CORRECT BOT
======================
Strategy: 
1. WAIT for price to reach 90c (meaning high certainty winner)
2. THEN buy at that price
3. Let settle at $1

CRITICAL: We do NOT place limit orders at 90c and wait.
          We WATCH prices and only buy when price IS at 90c.
"""

import json
import time
import asyncio
import aiohttp
from datetime import datetime
from typing import Optional, Dict
from web3 import Web3

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from py_clob_client.constants import POLYGON

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# === STRATEGY CONFIG ===
ENTRY_THRESHOLD = 0.90  # Only buy when mid-price >= 90c
BALANCE_PCT = 0.45      # 45% of balance per trade
MIN_SHARES = 5          # Polymarket minimum
POLL_MS = 500           # Check every 500ms
SETTLE_WAIT = 120       # Wait 2 min for settlement
MIN_TIME_SECS = 60      # Don't trade in last minute


class CorrectBot:
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
        
        # Balance reading
        self.w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        usdc_addr = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        usdc_abi = [{"constant":True,"inputs":[{"name":"account","type":"address"}],
                     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
        self.usdc = self.w3.eth.contract(address=Web3.to_checksum_address(usdc_addr), abi=usdc_abi)
        
        # State
        self.tokens = {}
        self.current_window = None
        self.holding = None
        self.holding_price = 0
        self.holding_shares = 0
        self.holding_cost = 0
        self.traded_this_window = False
        
        # Stats
        self.wins = 0
        self.losses = 0
        self.trades = []
    
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
    
    async def fetch_tokens(self, slug: str) -> Dict:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{GAMMA_API}/markets",
                    params={"slug": slug},
                    timeout=aiohttp.ClientTimeout(total=5)
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
    
    async def fetch_prices(self) -> Dict:
        """Get current mid prices"""
        if not self.tokens:
            return {}
        
        prices = {}
        try:
            async with aiohttp.ClientSession() as session:
                for side, token in self.tokens.items():
                    try:
                        async with session.get(
                            f"{CLOB_HOST}/midpoint",
                            params={"token_id": token},
                            timeout=aiohttp.ClientTimeout(total=2)
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                prices[side] = float(data.get("mid", 0))
                    except:
                        pass
        except:
            pass
        
        return prices
    
    async def fetch_best_ask(self, token: str) -> float:
        """Get best ask price for execution"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{CLOB_HOST}/book",
                    params={"token_id": token},
                    timeout=aiohttp.ClientTimeout(total=2)
                ) as resp:
                    if resp.status == 200:
                        book = await resp.json()
                        asks = book.get("asks", [])
                        if asks:
                            return float(asks[0].get("price", 0))
        except:
            pass
        return 0
    
    def execute_buy(self, side: str, price: float, amount: float) -> bool:
        """Execute buy order at specific price"""
        token = self.tokens.get(side)
        if not token:
            return False
        
        shares = amount / price
        if shares < MIN_SHARES:
            self.log(f"  Shares {shares:.1f} < min {MIN_SHARES}")
            return False
        
        self.log(f"  BUYING {side.upper()}: {shares:.1f} shares @ {price*100:.1f}c")
        
        try:
            # Place limit order at the price we see
            args = OrderArgs(token_id=token, price=price, size=shares, side=BUY)
            signed = self.client.create_order(args)
            result = self.client.post_order(signed, OrderType.GTC)
            
            if result and result.get("success"):
                order_id = result.get("orderID")
                self.log(f"  ORDER OK: {order_id[:30]}...")
                
                # Check fill status
                time.sleep(2)
                try:
                    order = self.client.get_order(order_id)
                    if order:
                        matched = float(order.get("size_matched", 0))
                        status = str(order.get("status", "")).upper()
                        fill_price = float(order.get("price", price))
                        
                        if matched > 0 or status in ["MATCHED", "FILLED"]:
                            self.log(f"  FILLED: {matched:.1f} shares @ {fill_price*100:.1f}c")
                            self.holding = side
                            self.holding_price = fill_price
                            self.holding_shares = matched if matched > 0 else shares
                            self.holding_cost = self.holding_shares * self.holding_price
                            return True
                except:
                    pass
                
                # Assume filled if order succeeded
                self.log(f"  ASSUMED FILLED")
                self.holding = side
                self.holding_price = price
                self.holding_shares = shares
                self.holding_cost = shares * price
                return True
            else:
                self.log(f"  ORDER FAILED: {result}")
        except Exception as e:
            self.log(f"  ERROR: {str(e)[:60]}")
        
        return False
    
    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}")
    
    async def on_new_window(self, window: Dict):
        """Handle transition to new window"""
        
        # Settlement from previous holding
        if self.holding:
            self.log(f"WINDOW CLOSED - Held {self.holding.upper()} @ {self.holding_price*100:.1f}c")
            self.log(f"Waiting {SETTLE_WAIT}s for settlement...")
            
            balance_before = self.get_balance()
            await asyncio.sleep(SETTLE_WAIT)
            balance_after = self.get_balance()
            
            # Calculate result
            change = balance_after - balance_before
            expected_payout = self.holding_shares  # $1 per share if win
            
            if change > self.holding_cost * 0.5:
                self.wins += 1
                profit = expected_payout - self.holding_cost
                self.log(f"RESULT: WIN! +${profit:.2f}")
            else:
                self.losses += 1
                self.log(f"RESULT: LOSS -${self.holding_cost:.2f}")
            
            self.trades.append({
                "window": self.current_window,
                "side": self.holding,
                "price": self.holding_price,
                "shares": self.holding_shares,
                "cost": self.holding_cost,
                "change": change
            })
            
            self.holding = None
            self.holding_price = 0
            self.holding_shares = 0
            self.holding_cost = 0
        
        # Cancel any leftover orders
        self.cancel_all()
        
        # Setup new window
        self.current_window = window["slug"]
        self.tokens = await self.fetch_tokens(self.current_window)
        self.traded_this_window = False
        
        balance = self.get_balance()
        min_required = MIN_SHARES * ENTRY_THRESHOLD
        
        self.log(f"NEW WINDOW: {window['slug']}")
        self.log(f"Balance: ${balance:.2f}")
        
        if balance < min_required:
            self.log(f"WARNING: Need ${min_required:.2f} minimum")
    
    async def run(self, duration_hours: float = 12):
        self.log("=" * 60)
        self.log("POLYMARKET CORRECT BOT")
        self.log("=" * 60)
        self.log(f"Proxy: {self.proxy}")
        self.log(f"Strategy: WAIT for price >= {ENTRY_THRESHOLD*100:.0f}c, then buy")
        self.log(f"Poll interval: {POLL_MS}ms")
        self.log(f"Duration: {duration_hours}h")
        
        self.cancel_all()
        
        balance = self.get_balance()
        starting_balance = balance
        self.log(f"Starting balance: ${balance:.2f}")
        self.log("=" * 60)
        
        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        last_window = None
        last_status = 0
        
        try:
            while time.time() < deadline:
                window = self.get_window()
                
                # New window?
                if window["slug"] != last_window:
                    await self.on_new_window(window)
                    last_window = window["slug"]
                
                # Get current prices
                prices = await self.fetch_prices()
                up = prices.get("up", 0)
                dn = prices.get("down", 0)
                
                # Entry logic: ONLY if not traded and price is high
                if not self.traded_this_window and not self.holding and self.tokens:
                    if window["secs_left"] >= MIN_TIME_SECS:
                        
                        # Check UP
                        if up >= ENTRY_THRESHOLD:
                            self.log(f"*** SIGNAL: UP @ {up*100:.0f}c >= {ENTRY_THRESHOLD*100:.0f}c ***")
                            
                            balance = self.get_balance()
                            trade_amount = balance * BALANCE_PCT
                            
                            # Get actual ask price for execution
                            ask = await self.fetch_best_ask(self.tokens["up"])
                            if ask > 0 and ask <= up + 0.01:  # Within 1c of mid
                                if self.execute_buy("up", ask, trade_amount):
                                    self.traded_this_window = True
                        
                        # Check DOWN
                        elif dn >= ENTRY_THRESHOLD:
                            self.log(f"*** SIGNAL: DOWN @ {dn*100:.0f}c >= {ENTRY_THRESHOLD*100:.0f}c ***")
                            
                            balance = self.get_balance()
                            trade_amount = balance * BALANCE_PCT
                            
                            # Get actual ask price for execution
                            ask = await self.fetch_best_ask(self.tokens["down"])
                            if ask > 0 and ask <= dn + 0.01:
                                if self.execute_buy("down", ask, trade_amount):
                                    self.traded_this_window = True
                
                # Status update (every 2 sec)
                now = time.time()
                if now - last_status >= 2:
                    if self.holding:
                        status = f"HOLD {self.holding.upper()}@{self.holding_price*100:.0f}c"
                    elif self.traded_this_window:
                        status = "WAIT"
                    else:
                        status = "SCAN"
                    
                    balance = self.get_balance()
                    pnl = balance - starting_balance
                    
                    print(f"\r  [{window['time_str']}] Up:{up*100:.0f}c Dn:{dn*100:.0f}c | {status} | W{self.wins} L{self.losses} | ${balance:.2f} ({pnl:+.2f})", end="", flush=True)
                    last_status = now
                
                # Poll interval
                await asyncio.sleep(POLL_MS / 1000)
        
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
        self.log("FINAL SUMMARY")
        self.log("=" * 60)
        self.log(f"Starting: ${starting_balance:.2f}")
        self.log(f"Final:    ${balance:.2f}")
        self.log(f"P&L:      ${pnl:+.2f}")
        self.log(f"Wins:     {self.wins}")
        self.log(f"Losses:   {self.losses}")
        if self.wins + self.losses > 0:
            self.log(f"Win rate: {self.wins/(self.wins+self.losses)*100:.0f}%")
        
        with open("pm_correct_results.json", "w") as f:
            json.dump({
                "starting": starting_balance,
                "final": balance,
                "pnl": pnl,
                "wins": self.wins,
                "losses": self.losses,
                "trades": self.trades
            }, f, indent=2)


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=12, help="Hours to run")
    args = p.parse_args()
    
    bot = CorrectBot()
    asyncio.run(bot.run(duration_hours=args.duration))


if __name__ == "__main__":
    main()

