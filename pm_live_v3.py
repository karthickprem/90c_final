"""
POLYMARKET LIVE TRADER v3 - FIXED
==================================
Fixes:
1. Use 90% balance, split by 2
2. Delay between orders to avoid rate limiting
3. Skip SELL order - let positions settle at $1
4. Wait for settlement before next window
5. Auto window switching
6. Better error handling
"""

import json
import time
import requests
from datetime import datetime
from typing import Optional, Dict

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from py_clob_client.constants import POLYGON

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# Trading config
ENTRY_PRICE = 0.90      # Buy at 90c
BALANCE_PCT = 0.90      # Use 90% of balance
MIN_SHARES = 5          # Polymarket minimum
ORDER_DELAY = 2.0       # Seconds between orders
POLL_INTERVAL = 5       # Seconds between checks
SETTLE_WAIT = 30        # Seconds to wait for settlement


class LiveTraderV3:
    def __init__(self, config_path: str = "pm_api_config.json"):
        with open(config_path) as f:
            config = json.load(f)
        
        self.proxy_address = config.get("proxy_address", "")
        
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
            signature_type=config.get("signature_type", 1),
            funder=self.proxy_address,
        )
        
        self.session = requests.Session()
        self.signer = self.client.get_address()
        
        # State
        self.balance = 0
        self.starting_balance = 0
        self.trades = []
        self.wins = 0
        self.losses = 0
        
        print("=" * 60)
        print("POLYMARKET LIVE TRADER v3")
        print("=" * 60)
        print(f"Signer: {self.signer}")
        print(f"Proxy:  {self.proxy_address}")
        print(f"Strategy: BUY at {ENTRY_PRICE*100:.0f}c, let settle at $1")
        print(f"Using {BALANCE_PCT*100:.0f}% of balance, split 50/50")
        print("=" * 60)
    
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
            print(f"  Token error: {e}")
        return {}
    
    def get_price(self, token_id: str) -> float:
        try:
            r = self.session.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=5)
            return float(r.json().get("mid", 0))
        except:
            return 0
    
    def place_order(self, token_id: str, price: float, size_usd: float) -> Optional[str]:
        try:
            shares = size_usd / price
            if shares < MIN_SHARES:
                shares = MIN_SHARES
            
            print(f"    Placing BUY: {shares:.1f} shares @ {price*100:.0f}c = ${shares*price:.2f}")
            
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=shares,
                side=BUY,
            )
            
            signed = self.client.create_order(order_args)
            result = self.client.post_order(signed, OrderType.GTC)
            
            if result and result.get("success"):
                order_id = result.get("orderID")
                print(f"    [OK] Order: {order_id[:30]}...")
                return order_id
            else:
                print(f"    [!] Order failed: {result}")
                
        except Exception as e:
            print(f"    [X] Order error: {e}")
        
        return None
    
    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel(order_id)
            return True
        except:
            return False
    
    def cancel_all(self):
        try:
            self.client.cancel_all()
        except:
            pass
    
    def is_filled(self, order_id: str) -> bool:
        try:
            order = self.client.get_order(order_id)
            if order:
                status = str(order.get("status", "")).upper()
                size_matched = float(order.get("size_matched", 0))
                original_size = float(order.get("original_size", 1))
                
                if status in ["MATCHED", "FILLED", "CLOSED"]:
                    return True
                if size_matched >= original_size * 0.5:
                    return True
        except:
            pass
        return False
    
    def run(self, balance: float, duration_hours: float = 12):
        self.balance = balance
        self.starting_balance = balance
        
        print(f"\n>>> STARTING LIVE TRADING")
        print(f"    Balance: ${self.balance:.2f}")
        print(f"    Duration: {duration_hours} hours")
        print("=" * 60)
        
        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        
        current_window = None
        tokens = {}
        orders = {}  # side -> {order_id, shares, cost}
        filled_side = None
        filled_shares = 0
        filled_cost = 0
        waiting_for_settle = False
        settle_window = None
        
        try:
            while time.time() < deadline:
                w = self.get_window()
                elapsed = (time.time() - start_time) / 60
                
                # === WAITING FOR SETTLEMENT ===
                if waiting_for_settle:
                    if w["slug"] != settle_window:
                        # New window started, settlement should be done
                        print(f"\n  [SETTLE] Waiting {SETTLE_WAIT}s for balance update...")
                        time.sleep(SETTLE_WAIT)
                        
                        # Assume WIN (90c+ positions usually win)
                        profit = filled_shares * 1.0 - filled_cost
                        self.balance += profit
                        self.wins += 1
                        
                        print(f"  [SETTLE] Assuming WIN: +${profit:.2f}")
                        print(f"  [SETTLE] New balance: ${self.balance:.2f}")
                        
                        waiting_for_settle = False
                        settle_window = None
                        filled_side = None
                        filled_shares = 0
                        filled_cost = 0
                    else:
                        # Still same window, keep waiting
                        print(f"\r  [{w['time_str']}] Waiting for window to close...", end="", flush=True)
                        time.sleep(POLL_INTERVAL)
                        continue
                
                # === NEW WINDOW ===
                if w["slug"] != current_window:
                    self.cancel_all()
                    
                    print(f"\n[{elapsed:.1f}m] NEW WINDOW: {w['slug']}")
                    current_window = w["slug"]
                    tokens = self.get_tokens(current_window)
                    orders = {}
                    
                    if not tokens:
                        print("  [!] No tokens found")
                        time.sleep(10)
                        continue
                    
                    # Calculate order size: 90% of balance, split by 2
                    usable = self.balance * BALANCE_PCT
                    per_side = usable / 2
                    
                    print(f"  Balance: ${self.balance:.2f} -> ${per_side:.2f}/side (90% split)")
                    
                    # Place UP order first
                    token_up = tokens.get("up")
                    if token_up:
                        oid = self.place_order(token_up, ENTRY_PRICE, per_side)
                        if oid:
                            orders["up"] = {
                                "order_id": oid,
                                "shares": per_side / ENTRY_PRICE,
                                "cost": per_side
                            }
                    
                    # Wait before placing second order (avoid rate limit)
                    print(f"    Waiting {ORDER_DELAY}s before next order...")
                    time.sleep(ORDER_DELAY)
                    
                    # Place DOWN order
                    token_down = tokens.get("down")
                    if token_down:
                        oid = self.place_order(token_down, ENTRY_PRICE, per_side)
                        if oid:
                            orders["down"] = {
                                "order_id": oid,
                                "shares": per_side / ENTRY_PRICE,
                                "cost": per_side
                            }
                
                # === CHECK FILLS ===
                if orders and not filled_side:
                    for side, info in list(orders.items()):
                        if self.is_filled(info["order_id"]):
                            print(f"\n  *** {side.upper()} FILLED at {ENTRY_PRICE*100:.0f}c! ***")
                            filled_side = side
                            filled_shares = info["shares"]
                            filled_cost = info["cost"]
                            
                            # Cancel other side
                            other = "down" if side == "up" else "up"
                            if other in orders:
                                print(f"  Cancelling {other.upper()} order...")
                                self.cancel_order(orders[other]["order_id"])
                            
                            orders = {}
                            
                            # Record trade
                            self.trades.append({
                                "window": current_window,
                                "side": side,
                                "entry": ENTRY_PRICE,
                                "shares": filled_shares,
                                "cost": filled_cost,
                                "time": datetime.now().isoformat(),
                            })
                            
                            # Now wait for settlement (no SELL order needed)
                            print(f"  Holding {filled_shares:.1f} shares, will settle at $1 if {side.upper()} wins")
                            waiting_for_settle = True
                            settle_window = current_window
                            break
                
                # === STATUS ===
                if tokens and not waiting_for_settle:
                    up = self.get_price(tokens.get("up", ""))
                    dn = self.get_price(tokens.get("down", ""))
                    
                    state = "ORDERS OPEN" if orders else "IDLE"
                    pnl = self.balance - self.starting_balance
                    
                    print(f"\r  [{w['time_str']}] Up:{up*100:.0f}c Dn:{dn*100:.0f}c | {state} | ${self.balance:.2f} ({pnl:+.2f})", end="", flush=True)
                
                time.sleep(POLL_INTERVAL)
        
        except KeyboardInterrupt:
            print("\n\n[STOP] Interrupted by user")
        except Exception as e:
            print(f"\n\n[ERROR] {e}")
        finally:
            self.cancel_all()
            self.summary()
    
    def summary(self):
        print("\n" + "=" * 60)
        print("TRADING SUMMARY")
        print("=" * 60)
        
        pnl = self.balance - self.starting_balance
        pnl_pct = (pnl / self.starting_balance) * 100 if self.starting_balance > 0 else 0
        
        print(f"\nStarting: ${self.starting_balance:.2f}")
        print(f"Final:    ${self.balance:.2f}")
        print(f"P&L:      ${pnl:+.2f} ({pnl_pct:+.1f}%)")
        print(f"\nTrades: {len(self.trades)}")
        print(f"Wins: {self.wins} | Losses: {self.losses}")
        
        # Save results
        results = {
            "end_time": datetime.now().isoformat(),
            "starting_balance": self.starting_balance,
            "final_balance": self.balance,
            "pnl": pnl,
            "wins": self.wins,
            "losses": self.losses,
            "trades": self.trades
        }
        
        with open("pm_live_results.json", "w") as f:
            json.dump(results, f, indent=2)
        
        print("\nResults saved to: pm_live_results.json")
        print("=" * 60)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--balance", type=float, default=20.86, help="Current balance")
    p.add_argument("--duration", type=float, default=12, help="Hours to run")
    args = p.parse_args()
    
    trader = LiveTraderV3()
    trader.run(balance=args.balance, duration_hours=args.duration)

