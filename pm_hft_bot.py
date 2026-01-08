"""
POLYMARKET HFT BOT
==================
High-frequency trading with:
- 100-200ms polling interval
- Async price fetching
- Immediate order execution
- Minimal latency
"""

import json
import time
import asyncio
import aiohttp
from datetime import datetime
from typing import Optional, Dict
from web3 import Web3
from concurrent.futures import ThreadPoolExecutor

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from py_clob_client.constants import POLYGON

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# HFT Config
POLL_INTERVAL_MS = 200      # 200ms = 5 checks per second
ENTRY_THRESHOLD = 0.90      # Buy at 90c
BALANCE_PCT = 0.45          # 45% per trade
MIN_SHARES = 5
SETTLE_WAIT = 120
MIN_TIME_TO_TRADE = 30      # Don't trade in last 30 seconds


class HFTBot:
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
        
        # Web3 for balance (cached)
        self.w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        usdc_addr = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        usdc_abi = [{"constant":True,"inputs":[{"name":"account","type":"address"}],
                     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
        self.usdc = self.w3.eth.contract(address=Web3.to_checksum_address(usdc_addr), abi=usdc_abi)
        
        # Thread pool for blocking operations
        self.executor = ThreadPoolExecutor(max_workers=4)
        
        # State
        self.tokens = {}
        self.current_window = None
        self.holding = None
        self.holding_cost = 0
        self.traded_this_window = False
        self.balance_cache = 0
        self.last_balance_check = 0
        
        # Stats
        self.trades = []
        self.wins = 0
        self.losses = 0
        self.ticks = 0
        self.last_tick_time = 0
    
    def get_balance(self) -> float:
        """Cached balance check"""
        now = time.time()
        if now - self.last_balance_check > 30:  # Refresh every 30s
            try:
                bal = self.usdc.functions.balanceOf(Web3.to_checksum_address(self.proxy)).call()
                self.balance_cache = bal / 1e6
                self.last_balance_check = now
            except:
                pass
        return self.balance_cache
    
    def get_balance_fresh(self) -> float:
        """Force fresh balance"""
        try:
            bal = self.usdc.functions.balanceOf(Web3.to_checksum_address(self.proxy)).call()
            self.balance_cache = bal / 1e6
            self.last_balance_check = time.time()
        except:
            pass
        return self.balance_cache
    
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
        """Async token fetch"""
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
    
    async def fetch_prices_fast(self) -> Dict:
        """Async parallel price fetch - FAST"""
        if not self.tokens:
            return {}
        
        prices = {}
        try:
            async with aiohttp.ClientSession() as session:
                tasks = []
                for side, token in self.tokens.items():
                    task = self.fetch_single_price(session, side, token)
                    tasks.append(task)
                
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, dict):
                        prices.update(result)
        except:
            pass
        
        return prices
    
    async def fetch_single_price(self, session, side: str, token: str) -> Dict:
        """Fetch single price"""
        try:
            async with session.get(
                f"{CLOB_HOST}/midpoint",
                params={"token_id": token},
                timeout=aiohttp.ClientTimeout(total=1)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {side: float(data.get("mid", 0))}
        except:
            pass
        return {}
    
    def execute_buy(self, side: str, price: float, amount: float) -> bool:
        """Synchronous buy execution"""
        token = self.tokens.get(side)
        if not token:
            return False
        
        shares = amount / price
        if shares < MIN_SHARES:
            return False
        
        try:
            args = OrderArgs(token_id=token, price=price, size=shares, side=BUY)
            signed = self.client.create_order(args)
            result = self.client.post_order(signed, OrderType.GTC)
            
            if result and result.get("success"):
                self.log(f"BUY {side.upper()} @ {price*100:.0f}c - {shares:.1f} shares - OK")
                return True
            else:
                self.log(f"BUY FAILED: {result}")
        except Exception as e:
            self.log(f"BUY ERROR: {str(e)[:50]}")
        
        return False
    
    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts}] {msg}")
    
    async def on_new_window(self, window: Dict):
        """Handle new window"""
        
        # Settlement
        if self.holding:
            self.log(f"SETTLE: Waiting {SETTLE_WAIT}s...")
            
            balance_before = self.get_balance_fresh()
            await asyncio.sleep(SETTLE_WAIT)
            balance_after = self.get_balance_fresh()
            
            change = balance_after - balance_before
            
            if change > 0:
                self.wins += 1
                self.log(f"SETTLE: WIN +${change:.2f}")
            else:
                self.losses += 1
                self.log(f"SETTLE: LOSS")
            
            self.trades.append({
                "window": self.current_window,
                "side": self.holding,
                "change": change
            })
            
            self.holding = None
            self.holding_cost = 0
        
        self.cancel_all()
        
        # Setup new window
        self.current_window = window["slug"]
        self.tokens = await self.fetch_tokens(self.current_window)
        self.traded_this_window = False
        self.get_balance_fresh()
        
        self.log(f"NEW WINDOW: {window['slug']} | Balance: ${self.balance_cache:.2f}")
    
    async def run(self, duration_hours: float = 12):
        self.log("=" * 60)
        self.log("POLYMARKET HFT BOT")
        self.log("=" * 60)
        self.log(f"Proxy: {self.proxy}")
        self.log(f"Poll interval: {POLL_INTERVAL_MS}ms")
        self.log(f"Entry threshold: {ENTRY_THRESHOLD*100:.0f}c")
        self.log(f"Duration: {duration_hours}h")
        
        self.cancel_all()
        self.get_balance_fresh()
        starting_balance = self.balance_cache
        self.log(f"Starting balance: ${starting_balance:.2f}")
        self.log("=" * 60)
        
        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        last_window = None
        last_status = 0
        
        try:
            while time.time() < deadline:
                tick_start = time.time()
                
                window = self.get_window()
                
                # New window?
                if window["slug"] != last_window:
                    await self.on_new_window(window)
                    last_window = window["slug"]
                
                # Skip if no tokens or already traded
                if not self.tokens or self.traded_this_window or self.holding:
                    await asyncio.sleep(POLL_INTERVAL_MS / 1000)
                    continue
                
                # Check time
                if window["secs_left"] < MIN_TIME_TO_TRADE:
                    await asyncio.sleep(POLL_INTERVAL_MS / 1000)
                    continue
                
                # FAST price fetch
                prices = await self.fetch_prices_fast()
                up = prices.get("up", 0)
                dn = prices.get("down", 0)
                
                self.ticks += 1
                
                # Check for entry
                if up >= ENTRY_THRESHOLD:
                    self.log(f"*** SIGNAL: UP @ {up*100:.0f}c ***")
                    
                    trade_amount = self.balance_cache * BALANCE_PCT
                    if self.execute_buy("up", up, trade_amount):
                        self.holding = "up"
                        self.holding_cost = trade_amount
                        self.traded_this_window = True
                        
                elif dn >= ENTRY_THRESHOLD:
                    self.log(f"*** SIGNAL: DOWN @ {dn*100:.0f}c ***")
                    
                    trade_amount = self.balance_cache * BALANCE_PCT
                    if self.execute_buy("down", dn, trade_amount):
                        self.holding = "down"
                        self.holding_cost = trade_amount
                        self.traded_this_window = True
                
                # Status update (every 2 seconds)
                now = time.time()
                if now - last_status >= 2:
                    tick_rate = self.ticks / (now - start_time) if now > start_time else 0
                    
                    if self.holding:
                        status = f"HOLD {self.holding.upper()}"
                    else:
                        status = "SCAN"
                    
                    print(f"\r  [{window['time_str']}] Up:{up*100:.0f}c Dn:{dn*100:.0f}c | {status} | {tick_rate:.1f} t/s | ${self.balance_cache:.2f}", end="", flush=True)
                    last_status = now
                
                # Precise timing
                elapsed = time.time() - tick_start
                sleep_time = max(0, (POLL_INTERVAL_MS / 1000) - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
        
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
        balance = self.get_balance_fresh()
        pnl = balance - starting_balance
        
        self.log("\n" + "=" * 60)
        self.log("SUMMARY")
        self.log("=" * 60)
        self.log(f"Starting: ${starting_balance:.2f}")
        self.log(f"Final:    ${balance:.2f}")
        self.log(f"P&L:      ${pnl:+.2f}")
        self.log(f"Ticks:    {self.ticks}")
        self.log(f"Wins:     {self.wins}")
        self.log(f"Losses:   {self.losses}")
        
        with open("pm_hft_results.json", "w") as f:
            json.dump({
                "starting": starting_balance,
                "final": balance,
                "pnl": pnl,
                "ticks": self.ticks,
                "wins": self.wins,
                "losses": self.losses,
                "trades": self.trades
            }, f, indent=2)


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=12, help="Hours")
    args = p.parse_args()
    
    bot = HFTBot()
    asyncio.run(bot.run(duration_hours=args.duration))


if __name__ == "__main__":
    main()

