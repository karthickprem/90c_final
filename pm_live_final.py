"""
POLYMARKET LIVE TRADER - FINAL VERSION
=======================================
Strategy: Buy at 90c for both sides, sell at 99c, compound

TESTED AND WORKING!
"""

import json
import time
import requests
from datetime import datetime
from typing import Optional, Dict

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.constants import POLYGON

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# Trading config
ENTRY_PRICE = 0.90      # Buy at 90c
EXIT_PRICE = 0.99       # Sell at 99c
MIN_SHARES = 5          # Polymarket minimum
POLL_INTERVAL = 3       # Seconds between checks


class LiveTrader:
    def __init__(self, config_path: str = "pm_api_config.json"):
        # Load config
        with open(config_path) as f:
            config = json.load(f)
        
        self.proxy_address = config.get("proxy_address", "")
        
        # Create credentials
        creds = ApiCreds(
            api_key=config["api_key"],
            api_secret=config["api_secret"],
            api_passphrase=config["api_passphrase"],
        )
        
        # Initialize client with CORRECT settings
        self.client = ClobClient(
            host=CLOB_HOST,
            key=config["private_key"],
            chain_id=POLYGON,
            creds=creds,
            signature_type=config.get("signature_type", 1),
            funder=self.proxy_address,
        )
        
        self.session = requests.Session()
        self.signer = self.client.get_address()
        
        # Trading state
        self.balance = 19.12  # Will compound
        self.trades = []
        self.starting_balance = self.balance
        
        print("=" * 60)
        print("POLYMARKET LIVE TRADER - FINAL")
        print("=" * 60)
        print(f"Signer: {self.signer}")
        print(f"Proxy:  {self.proxy_address}")
        print(f"Balance: ${self.balance:.2f}")
        print("=" * 60)
    
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
        except Exception as e:
            print(f"  Token error: {e}")
        return {}
    
    def get_price(self, token_id: str) -> float:
        try:
            r = self.session.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=5)
            return float(r.json().get("mid", 0))
        except:
            return 0
    
    def place_order(self, token_id: str, side: str, price: float, amount_usd: float) -> Optional[str]:
        try:
            # Calculate shares
            shares = amount_usd / price
            
            # Ensure minimum
            if shares < MIN_SHARES:
                shares = MIN_SHARES
            
            print(f"    Placing {side}: {shares:.1f} shares @ {price*100:.0f}c = ${shares*price:.2f}")
            
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=shares,
                side=BUY if side.upper() == "BUY" else SELL,
            )
            
            signed = self.client.create_order(order_args)
            result = self.client.post_order(signed, OrderType.GTC)
            
            if result and result.get("success"):
                order_id = result.get("orderID")
                print(f"    [OK] Order: {order_id[:30]}...")
                return order_id
            else:
                print(f"    [!] Result: {result}")
                
        except Exception as e:
            print(f"    [X] Order error: {e}")
        
        return None
    
    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel(order_id)
            return True
        except:
            return False
    
    def is_filled(self, order_id: str) -> bool:
        try:
            order = self.client.get_order(order_id)
            if order:
                status = str(order.get("status", "")).upper()
                size_matched = float(order.get("size_matched", 0))
                original_size = float(order.get("original_size", 1))
                
                # Check if fully or mostly filled
                if status in ["MATCHED", "FILLED", "CLOSED"]:
                    return True
                if size_matched >= original_size * 0.9:  # 90%+ filled
                    return True
        except:
            pass
        return False
    
    def cancel_all(self):
        try:
            self.client.cancel_all()
            print("  [OK] All orders cancelled")
        except Exception as e:
            print(f"  Cancel error: {e}")
    
    def run(self, duration_hours: float = 12):
        print(f"\n>>> STARTING LIVE TRADING for {duration_hours} hours")
        print(f"    Strategy: BUY at {ENTRY_PRICE*100:.0f}c, SELL at {EXIT_PRICE*100:.0f}c")
        print(f"    Balance: ${self.balance:.2f} (compounding)")
        print("=" * 60)
        
        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        
        current_slug = None
        tokens = {}
        buy_orders = {}  # side -> order_id
        sell_order = None
        holding = None  # "up" or "down"
        entry_shares = 0
        
        try:
            while time.time() < deadline:
                w = self.get_window()
                elapsed = (time.time() - start_time) / 60
                
                # === NEW WINDOW ===
                if w["slug"] != current_slug:
                    self.cancel_all()
                    
                    print(f"\n[{elapsed:.1f}m] NEW WINDOW: {w['slug']}")
                    current_slug = w["slug"]
                    tokens = self.get_tokens(current_slug)
                    buy_orders = {}
                    sell_order = None
                    holding = None
                    entry_shares = 0
                    
                    if not tokens:
                        print("  [!] No tokens")
                        time.sleep(10)
                        continue
                    
                    # Split balance 50/50
                    trade_size = self.balance / 2
                    print(f"  Balance: ${self.balance:.2f} -> ${trade_size:.2f}/side")
                    
                    # Place BUY orders at 90c for both sides
                    for side in ["up", "down"]:
                        token = tokens.get(side)
                        if token:
                            oid = self.place_order(token, "BUY", ENTRY_PRICE, trade_size)
                            if oid:
                                buy_orders[side] = oid
                
                # === CHECK BUY FILLS ===
                if buy_orders and not holding:
                    for side, oid in list(buy_orders.items()):
                        if self.is_filled(oid):
                            print(f"\n  *** {side.upper()} FILLED at {ENTRY_PRICE*100:.0f}c! ***")
                            holding = side
                            entry_shares = (self.balance / 2) / ENTRY_PRICE
                            
                            # Cancel other side
                            other = "down" if side == "up" else "up"
                            if other in buy_orders:
                                self.cancel_order(buy_orders[other])
                            buy_orders = {}
                            
                            # Place SELL at 99c
                            token = tokens.get(side)
                            if token:
                                sell_order = self.place_order(
                                    token, "SELL", EXIT_PRICE, 
                                    entry_shares * EXIT_PRICE
                                )
                            
                            self.trades.append({
                                "window": current_slug,
                                "side": side,
                                "entry": ENTRY_PRICE,
                                "shares": entry_shares,
                                "time": time.time(),
                                "status": "holding"
                            })
                            break
                
                # === CHECK SELL FILL ===
                if sell_order and self.is_filled(sell_order):
                    print(f"\n  *** SOLD at {EXIT_PRICE*100:.0f}c! ***")
                    
                    # Calculate profit
                    profit = (EXIT_PRICE - ENTRY_PRICE) * entry_shares
                    self.balance += profit
                    
                    print(f"  Profit: +${profit:.2f}")
                    print(f"  New balance: ${self.balance:.2f}")
                    
                    sell_order = None
                    holding = None
                    
                    if self.trades:
                        self.trades[-1]["status"] = "closed"
                        self.trades[-1]["exit"] = EXIT_PRICE
                        self.trades[-1]["profit"] = profit
                
                # === STATUS ===
                if tokens:
                    up = self.get_price(tokens.get("up", ""))
                    dn = self.get_price(tokens.get("down", ""))
                    
                    state = "WAITING"
                    if holding:
                        state = f"HOLD {holding.upper()}"
                    elif buy_orders:
                        state = "ORDERS OPEN"
                    
                    pnl = self.balance - self.starting_balance
                    print(f"\r  [{w['time_str']}] Up:{up*100:.0f}c Dn:{dn*100:.0f}c | {state} | ${self.balance:.2f} ({pnl:+.2f})", end="", flush=True)
                
                time.sleep(POLL_INTERVAL)
        
        except KeyboardInterrupt:
            print("\n\n[STOP] Stopped by user")
        
        finally:
            self.cancel_all()
            self.summary()
    
    def summary(self):
        print("\n" + "=" * 60)
        print("TRADING SUMMARY")
        print("=" * 60)
        
        pnl = self.balance - self.starting_balance
        pnl_pct = (pnl / self.starting_balance) * 100
        
        print(f"\nStarting: ${self.starting_balance:.2f}")
        print(f"Final:    ${self.balance:.2f}")
        print(f"P&L:      ${pnl:+.2f} ({pnl_pct:+.1f}%)")
        
        print(f"\nTotal trades: {len(self.trades)}")
        
        closed = [t for t in self.trades if t.get("status") == "closed"]
        if closed:
            wins = [t for t in closed if t.get("profit", 0) > 0]
            total_profit = sum(t.get("profit", 0) for t in closed)
            print(f"Closed: {len(closed)}")
            print(f"Wins: {len(wins)}")
            print(f"Profit from trades: ${total_profit:.2f}")
        
        # Save results
        results = {
            "end_time": datetime.now().isoformat(),
            "starting_balance": self.starting_balance,
            "final_balance": self.balance,
            "pnl": pnl,
            "trades": self.trades
        }
        
        with open("pm_live_results.json", "w") as f:
            json.dump(results, f, indent=2)
        
        print("\nResults saved to: pm_live_results.json")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=12, help="Hours to run")
    p.add_argument("--balance", type=float, default=19.12, help="Starting balance")
    args = p.parse_args()
    
    trader = LiveTrader()
    trader.balance = args.balance
    trader.starting_balance = args.balance
    
    trader.run(duration_hours=args.duration)

