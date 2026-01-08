"""
POLYMARKET FINAL BOT
====================
Strategy:
1. WATCH prices continuously (Up and Down)
2. WAIT until EITHER price >= 90c
3. BUY that side with 90% of balance
4. After window closes -> WAIT for settlement
5. Read new balance (with profits) and repeat

NO splitting. ONE trade per window. Full 90% on winning side.
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

# === CONFIG ===
ENTRY_THRESHOLD = 0.90  # Buy when price >= 90c
BALANCE_PCT = 0.90      # Use 90% of balance
MIN_SHARES = 5          # Polymarket minimum
POLL_MS = 300           # Check every 300ms (fast)
SETTLE_WAIT = 120       # Wait 2 min for settlement
MIN_TIME_SECS = 30      # Don't trade in last 30 seconds


class FinalBot:
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
        
        # State
        self.tokens = {}
        self.current_window = None
        
        # Position state
        self.holding_side = None      # "up" or "down"
        self.holding_price = 0        # Price we bought at
        self.holding_shares = 0       # Number of shares
        self.holding_cost = 0         # Total cost
        self.traded_this_window = False
        
        # Stats
        self.wins = 0
        self.losses = 0
        self.trades = []
        self.starting_balance = 0
    
    def get_balance(self) -> float:
        """Read actual USDC balance from blockchain"""
        try:
            bal = self.usdc.functions.balanceOf(Web3.to_checksum_address(self.proxy)).call()
            return bal / 1e6
        except Exception as e:
            self.log(f"Balance error: {e}")
            return 0
    
    def cancel_all(self):
        """Cancel all pending orders"""
        try:
            self.client.cancel_all()
        except:
            pass
    
    def get_window(self) -> Dict:
        """Get current 15-min window info"""
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
    
    async def fetch_tokens(self, slug: str) -> Dict:
        """Get token IDs for a market"""
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
        except Exception as e:
            self.log(f"Token fetch error: {e}")
        return {}
    
    async def fetch_prices(self) -> Dict:
        """Get mid prices for Up and Down"""
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
                        prices[side] = 0
        except:
            pass
        
        return prices
    
    async def fetch_best_ask(self, token: str) -> float:
        """Get best ask price from orderbook"""
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
    
    def execute_buy(self, side: str, price: float) -> bool:
        """Execute buy order with 90% of current balance"""
        token = self.tokens.get(side)
        if not token:
            self.log(f"  No token for {side}")
            return False
        
        # Get current balance
        balance = self.get_balance()
        trade_amount = balance * BALANCE_PCT
        
        shares = trade_amount / price
        if shares < MIN_SHARES:
            self.log(f"  Shares {shares:.1f} < minimum {MIN_SHARES}")
            return False
        
        self.log(f"  BUYING {side.upper()}")
        self.log(f"    Balance: ${balance:.2f}")
        self.log(f"    Using: ${trade_amount:.2f} (90%)")
        self.log(f"    Shares: {shares:.1f} @ {price*100:.1f}c")
        
        try:
            args = OrderArgs(token_id=token, price=price, size=shares, side=BUY)
            signed = self.client.create_order(args)
            result = self.client.post_order(signed, OrderType.GTC)
            
            if result and result.get("success"):
                order_id = result.get("orderID")
                self.log(f"    ORDER OK: {order_id[:30]}...")
                
                # Wait and check fill
                time.sleep(2)
                
                try:
                    order = self.client.get_order(order_id)
                    if order:
                        matched = float(order.get("size_matched", 0))
                        status = str(order.get("status", "")).upper()
                        
                        if matched > 0 or status in ["MATCHED", "FILLED"]:
                            actual_shares = matched if matched > 0 else shares
                            self.log(f"    FILLED: {actual_shares:.1f} shares")
                            
                            self.holding_side = side
                            self.holding_price = price
                            self.holding_shares = actual_shares
                            self.holding_cost = actual_shares * price
                            return True
                except:
                    pass
                
                # Assume filled
                self.log(f"    ASSUMED FILLED")
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
    
    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}")
    
    async def handle_settlement(self):
        """Wait for settlement and record result"""
        if not self.holding_side:
            return
        
        self.log(f"WINDOW CLOSED")
        self.log(f"  Position: {self.holding_side.upper()} @ {self.holding_price*100:.1f}c")
        self.log(f"  Shares: {self.holding_shares:.1f}")
        self.log(f"  Cost: ${self.holding_cost:.2f}")
        self.log(f"  Waiting {SETTLE_WAIT}s for settlement...")
        
        balance_before = self.get_balance()
        
        # Wait for settlement
        await asyncio.sleep(SETTLE_WAIT)
        
        balance_after = self.get_balance()
        change = balance_after - balance_before
        
        # Determine win/loss
        # If we win, we get $1 per share, so balance increases by (shares - cost)
        # If we lose, we get nothing, balance stays same (already paid)
        
        if balance_after > balance_before + 0.01:  # Got money back = WIN
            self.wins += 1
            profit = self.holding_shares - self.holding_cost  # Expected profit
            self.log(f"RESULT: WIN!")
            self.log(f"  Payout: ${self.holding_shares:.2f}")
            self.log(f"  Profit: +${profit:.2f}")
        else:
            self.losses += 1
            self.log(f"RESULT: LOSS")
            self.log(f"  Lost: -${self.holding_cost:.2f}")
        
        self.log(f"  New balance: ${balance_after:.2f}")
        
        # Record trade
        self.trades.append({
            "window": self.current_window,
            "side": self.holding_side,
            "price": self.holding_price,
            "shares": self.holding_shares,
            "cost": self.holding_cost,
            "balance_after": balance_after,
            "result": "WIN" if balance_after > balance_before + 0.01 else "LOSS"
        })
        
        # Clear position
        self.holding_side = None
        self.holding_price = 0
        self.holding_shares = 0
        self.holding_cost = 0
    
    async def setup_new_window(self, window: Dict):
        """Setup for new window"""
        self.cancel_all()
        
        self.current_window = window["slug"]
        self.tokens = await self.fetch_tokens(self.current_window)
        self.traded_this_window = False
        
        balance = self.get_balance()
        min_required = MIN_SHARES * ENTRY_THRESHOLD
        
        self.log(f"NEW WINDOW: {window['slug']}")
        self.log(f"  Balance: ${balance:.2f}")
        
        if not self.tokens:
            self.log(f"  ERROR: No tokens found")
        elif balance < min_required:
            self.log(f"  WARNING: Need ${min_required:.2f} minimum")
    
    async def run(self, duration_hours: float = 12):
        self.log("=" * 60)
        self.log("POLYMARKET FINAL BOT")
        self.log("=" * 60)
        self.log(f"Proxy: {self.proxy}")
        self.log("")
        self.log("STRATEGY:")
        self.log(f"  1. Watch Up and Down prices")
        self.log(f"  2. When EITHER >= {ENTRY_THRESHOLD*100:.0f}c -> BUY")
        self.log(f"  3. Use {BALANCE_PCT*100:.0f}% of balance")
        self.log(f"  4. Wait for settlement after window")
        self.log(f"  5. Compound with new balance")
        self.log("")
        self.log(f"Poll interval: {POLL_MS}ms")
        self.log(f"Duration: {duration_hours}h")
        
        self.cancel_all()
        
        self.starting_balance = self.get_balance()
        self.log(f"Starting balance: ${self.starting_balance:.2f}")
        self.log("=" * 60)
        
        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        last_window = None
        last_status = 0
        
        try:
            while time.time() < deadline:
                window = self.get_window()
                
                # === WINDOW TRANSITION ===
                if window["slug"] != last_window:
                    # Handle settlement from previous window
                    if self.holding_side:
                        await self.handle_settlement()
                    
                    # Setup new window
                    await self.setup_new_window(window)
                    last_window = window["slug"]
                
                # === GET PRICES ===
                prices = await self.fetch_prices()
                up = prices.get("up", 0)
                dn = prices.get("down", 0)
                
                # === ENTRY LOGIC ===
                if not self.traded_this_window and not self.holding_side and self.tokens:
                    if window["secs_left"] >= MIN_TIME_SECS:
                        
                        # Check if UP >= 90c
                        if up >= ENTRY_THRESHOLD:
                            self.log(f"*** SIGNAL: UP @ {up*100:.0f}c ***")
                            
                            # Get actual ask price (safe access)
                            up_token = self.tokens.get("up")
                            if up_token:
                                ask = await self.fetch_best_ask(up_token)
                                if ask >= ENTRY_THRESHOLD:
                                    if self.execute_buy("up", ask):
                                        self.traded_this_window = True
                                else:
                                    self.log(f"  Ask {ask*100:.0f}c < threshold, skipping")
                        
                        # Check if DOWN >= 90c
                        elif dn >= ENTRY_THRESHOLD:
                            self.log(f"*** SIGNAL: DOWN @ {dn*100:.0f}c ***")
                            
                            # Get actual ask price (safe access)
                            down_token = self.tokens.get("down")
                            if down_token:
                                ask = await self.fetch_best_ask(down_token)
                                if ask >= ENTRY_THRESHOLD:
                                    if self.execute_buy("down", ask):
                                        self.traded_this_window = True
                                else:
                                    self.log(f"  Ask {ask*100:.0f}c < threshold, skipping")
                
                # === STATUS UPDATE (every 2 sec) ===
                now = time.time()
                if now - last_status >= 2:
                    if self.holding_side:
                        status = f"HOLD {self.holding_side.upper()}@{self.holding_price*100:.0f}c"
                    else:
                        status = "SCAN"
                    
                    balance = self.get_balance()
                    pnl = balance - self.starting_balance
                    
                    print(f"\r  [{window['time_str']}] Up:{up*100:.0f}c Dn:{dn*100:.0f}c | {status} | W{self.wins}/L{self.losses} | ${balance:.2f} ({pnl:+.2f})", end="", flush=True)
                    last_status = now
                
                # === POLL INTERVAL ===
                await asyncio.sleep(POLL_MS / 1000)
        
        except KeyboardInterrupt:
            self.log("\n\nSTOPPED BY USER")
        except Exception as e:
            self.log(f"\n\nERROR: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.cancel_all()
            self.summary()
    
    def summary(self):
        balance = self.get_balance()
        pnl = balance - self.starting_balance
        
        self.log("\n" + "=" * 60)
        self.log("FINAL SUMMARY")
        self.log("=" * 60)
        self.log(f"Starting: ${self.starting_balance:.2f}")
        self.log(f"Final:    ${balance:.2f}")
        self.log(f"P&L:      ${pnl:+.2f}")
        self.log(f"Wins:     {self.wins}")
        self.log(f"Losses:   {self.losses}")
        if self.wins + self.losses > 0:
            self.log(f"Win rate: {self.wins/(self.wins+self.losses)*100:.0f}%")
        
        # Save results
        with open("pm_final_results.json", "w") as f:
            json.dump({
                "starting_balance": self.starting_balance,
                "final_balance": balance,
                "pnl": pnl,
                "wins": self.wins,
                "losses": self.losses,
                "trades": self.trades
            }, f, indent=2)
        
        self.log(f"\nResults saved to pm_final_results.json")


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=12, help="Hours to run")
    args = p.parse_args()
    
    bot = FinalBot()
    asyncio.run(bot.run(duration_hours=args.duration))


if __name__ == "__main__":
    main()

