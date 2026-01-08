"""
POLYMARKET LIVE TRADER V2 - BTC 15min Up/Down
==============================================
Strategy: Buy at 90c, Sell at 99c, Compound
"""

import json
import time
import requests
from datetime import datetime
from typing import Optional, Dict

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

ENTRY_PRICE = 0.90
EXIT_PRICE = 0.99
POLL_INTERVAL = 3


class LiveTraderV2:
    def __init__(self):
        with open("pm_api_config.json") as f:
            config = json.load(f)
        
        creds = ApiCreds(
            api_key=config["api_key"],
            api_secret=config["api_secret"],
            api_passphrase=config["api_passphrase"],
        )
        
        self.client = ClobClient(
            CLOB_HOST,
            key=config["private_key"],
            chain_id=CHAIN_ID,
            creds=creds
        )
        
        self.session = requests.Session()
        self.wallet_address = None
        
        try:
            self.wallet_address = self.client.get_address()
            print(f"Wallet: {self.wallet_address}")
        except Exception as e:
            print(f"Address error: {e}")
        
        self.balance = 19.0
        self.trades = []
        
        print("=" * 60)
        print("LIVE TRADER V2 INITIALIZED")
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
            print(f"Token error: {e}")
        return {}
    
    def get_price(self, token_id: str) -> float:
        try:
            r = self.session.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=5)
            return float(r.json().get("mid", 0))
        except:
            return 0
    
    def place_order(self, token_id: str, side: str, price: float, amount_usd: float) -> Optional[str]:
        try:
            size = amount_usd / price
            
            print(f"    Placing {side} order: {size:.2f} shares @ {price:.2f} = ${amount_usd:.2f}")
            
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY if side.upper() == "BUY" else SELL,
            )
            
            signed = self.client.create_order(order_args)
            result = self.client.post_order(signed, OrderType.GTC)
            
            if result:
                order_id = result.get("orderID") or result.get("order_id") or result.get("id")
                if order_id:
                    print(f"    [OK] Order placed: {order_id[:30]}...")
                    return order_id
                else:
                    print(f"    Order result: {result}")
            
        except Exception as e:
            print(f"    [X] Order error: {e}")
        
        return None
    
    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel(order_id)
            print(f"    [OK] Cancelled: {order_id[:30]}...")
            return True
        except Exception as e:
            print(f"    Cancel error: {e}")
            return False
    
    def is_filled(self, order_id: str) -> bool:
        try:
            order = self.client.get_order(order_id)
            if order:
                status = str(order.get("status", "")).upper()
                return status in ["MATCHED", "FILLED", "CLOSED"]
        except:
            pass
        return False
    
    def cancel_all(self):
        try:
            self.client.cancel_all()
            print("  [OK] All orders cancelled")
        except Exception as e:
            print(f"  Cancel all error: {e}")
    
    def run(self, duration_hours: float = 12):
        print(f"\n>>> Starting live trading for {duration_hours} hours")
        print(f"   Balance: ${self.balance:.2f}")
        print(f"   Entry: {ENTRY_PRICE*100:.0f}c | Exit: {EXIT_PRICE*100:.0f}c")
        print("=" * 60)
        
        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        
        current_slug = None
        tokens = {}
        buy_orders = {}
        sell_order = None
        holding = None
        
        try:
            while time.time() < deadline:
                w = self.get_window()
                elapsed = (time.time() - start_time) / 60
                
                if w["slug"] != current_slug:
                    self.cancel_all()
                    
                    print(f"\n[{elapsed:.1f}m] NEW WINDOW: {w['slug']}")
                    current_slug = w["slug"]
                    tokens = self.get_tokens(current_slug)
                    buy_orders = {}
                    sell_order = None
                    holding = None
                    
                    if not tokens:
                        print("  [!] No tokens, waiting...")
                        time.sleep(10)
                        continue
                    
                    trade_size = self.balance / 2
                    print(f"  Balance: ${self.balance:.2f} -> ${trade_size:.2f} per side")
                    
                    for side in ["up", "down"]:
                        token = tokens.get(side)
                        if token:
                            oid = self.place_order(token, "BUY", ENTRY_PRICE, trade_size)
                            if oid:
                                buy_orders[side] = oid
                
                if buy_orders and not holding:
                    for side, oid in list(buy_orders.items()):
                        if self.is_filled(oid):
                            print(f"\n  *** {side.upper()} FILLED! ***")
                            holding = side
                            
                            other = "down" if side == "up" else "up"
                            if other in buy_orders:
                                self.cancel_order(buy_orders[other])
                            buy_orders = {}
                            
                            token = tokens.get(side)
                            shares = (self.balance / 2) / ENTRY_PRICE
                            if token:
                                sell_order = self.place_order(token, "SELL", EXIT_PRICE, shares * EXIT_PRICE)
                            
                            self.trades.append({
                                "window": current_slug,
                                "side": side,
                                "entry": ENTRY_PRICE,
                                "time": time.time(),
                                "status": "holding"
                            })
                            break
                
                if sell_order and self.is_filled(sell_order):
                    print(f"\n  *** SOLD! Profit locked! ***")
                    profit = (EXIT_PRICE - ENTRY_PRICE) * (self.balance / 2) / ENTRY_PRICE
                    self.balance += profit
                    print(f"  New balance: ${self.balance:.2f} (+${profit:.2f})")
                    
                    sell_order = None
                    holding = None
                    
                    if self.trades:
                        self.trades[-1]["status"] = "closed"
                        self.trades[-1]["profit"] = profit
                
                if tokens:
                    up = self.get_price(tokens.get("up", ""))
                    dn = self.get_price(tokens.get("down", ""))
                    
                    state = "WAITING"
                    if holding:
                        state = f"HOLD {holding.upper()}"
                    elif buy_orders:
                        state = "ORDERS OPEN"
                    
                    print(f"\r  [{w['time_str']}] Up:{up*100:.0f}c Dn:{dn*100:.0f}c | {state} | ${self.balance:.2f}", end="", flush=True)
                
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
        print(f"Final balance: ${self.balance:.2f}")
        print(f"Total trades: {len(self.trades)}")
        
        closed = [t for t in self.trades if t.get("status") == "closed"]
        if closed:
            total_profit = sum(t.get("profit", 0) for t in closed)
            print(f"Closed trades: {len(closed)}")
            print(f"Total profit: ${total_profit:.2f}")
        
        with open("pm_live_results.json", "w") as f:
            json.dump({"balance": self.balance, "trades": self.trades}, f, indent=2)
        print("\nSaved to: pm_live_results.json")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=12)
    p.add_argument("--test", action="store_true")
    args = p.parse_args()
    
    trader = LiveTraderV2()
    
    if args.test:
        print("\n=== TEST MODE ===")
        w = trader.get_window()
        print(f"Window: {w['slug']}")
        print(f"Time: {w['time_str']}")
        tokens = trader.get_tokens(w["slug"])
        for s, t in tokens.items():
            print(f"  {s.upper()}: {trader.get_price(t)*100:.0f}c")
        
        print("\n[TEST] Testing order placement...")
        test_token = tokens.get("up")
        if test_token:
            oid = trader.place_order(test_token, "BUY", 0.01, 0.10)
            if oid:
                print("  [OK] Order placement works!")
                trader.cancel_order(oid)
            else:
                print("  [X] Order placement failed")
    else:
        trader.run(duration_hours=args.duration)
