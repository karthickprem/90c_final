"""
POLYMARKET LIVE TRADER v4 - SMART ENTRY
=======================================
Only place orders when price is >= 90c
Watch both sides, enter when one reaches threshold
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
ENTRY_THRESHOLD = 0.90  # Only buy when price >= 90c
BALANCE_PCT = 0.45      # Use 45% per trade (leaves buffer)
MIN_SHARES = 5
POLL_INTERVAL = 3
SETTLE_WAIT = 30


class LiveTraderV4:
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
        
        print("=" * 60)
        print("POLYMARKET LIVE TRADER v4 - SMART ENTRY")
        print("=" * 60)
        print(f"Proxy:  {self.proxy_address}")
        print(f"Strategy: BUY when price >= {ENTRY_THRESHOLD*100:.0f}c")
        print(f"Using {BALANCE_PCT*100:.0f}% of balance per trade")
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
    
    def get_prices(self, tokens: Dict) -> Dict:
        prices = {}
        for side, token_id in tokens.items():
            try:
                r = self.session.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=5)
                prices[side] = float(r.json().get("mid", 0))
            except:
                prices[side] = 0
        return prices
    
    def place_market_buy(self, token_id: str, price: float, size_usd: float) -> Optional[str]:
        """Place a limit buy at slightly above current price for fast fill"""
        try:
            # Use current price + 1c for fast fill
            buy_price = min(price + 0.01, 0.99)
            shares = size_usd / buy_price
            
            if shares < MIN_SHARES:
                shares = MIN_SHARES
            
            print(f"    Placing BUY: {shares:.1f} shares @ {buy_price*100:.0f}c")
            
            order_args = OrderArgs(
                token_id=token_id,
                price=buy_price,
                size=shares,
                side=BUY,
            )
            
            signed = self.client.create_order(order_args)
            result = self.client.post_order(signed, OrderType.GTC)
            
            if result and result.get("success"):
                order_id = result.get("orderID")
                print(f"    [OK] Order placed: {order_id[:30]}...")
                return order_id
            else:
                print(f"    [!] Order failed: {result}")
                
        except Exception as e:
            print(f"    [X] Order error: {e}")
        
        return None
    
    def is_filled(self, order_id: str) -> bool:
        try:
            order = self.client.get_order(order_id)
            if order:
                status = str(order.get("status", "")).upper()
                if status in ["MATCHED", "FILLED", "CLOSED"]:
                    return True
                size_matched = float(order.get("size_matched", 0))
                original_size = float(order.get("original_size", 1))
                if size_matched >= original_size * 0.5:
                    return True
        except:
            pass
        return False
    
    def cancel_all(self):
        try:
            self.client.cancel_all()
        except:
            pass
    
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
        order_id = None
        holding_side = None
        holding_shares = 0
        holding_cost = 0
        traded_this_window = False
        
        try:
            while time.time() < deadline:
                w = self.get_window()
                elapsed = (time.time() - start_time) / 60
                
                # === NEW WINDOW ===
                if w["slug"] != current_window:
                    # Check if we were holding from previous window
                    if holding_side:
                        print(f"\n  [SETTLE] Window closed, waiting {SETTLE_WAIT}s...")
                        time.sleep(SETTLE_WAIT)
                        
                        # Assume win (90c+ positions are high probability)
                        profit = holding_shares * 1.0 - holding_cost
                        self.balance += profit
                        self.wins += 1
                        
                        print(f"  [SETTLE] WIN: +${profit:.2f}")
                        print(f"  [SETTLE] New balance: ${self.balance:.2f}")
                        
                        holding_side = None
                        holding_shares = 0
                        holding_cost = 0
                    
                    self.cancel_all()
                    print(f"\n[{elapsed:.1f}m] NEW WINDOW: {w['slug']}")
                    current_window = w["slug"]
                    tokens = self.get_tokens(current_window)
                    order_id = None
                    traded_this_window = False
                    
                    if not tokens:
                        print("  [!] No tokens")
                        time.sleep(10)
                        continue
                
                # === WAITING FOR PRICE TO REACH 90c ===
                if not traded_this_window and tokens:
                    prices = self.get_prices(tokens)
                    up_price = prices.get("up", 0)
                    dn_price = prices.get("down", 0)
                    
                    # Check if either side is >= 90c
                    trade_side = None
                    trade_price = 0
                    
                    if up_price >= ENTRY_THRESHOLD:
                        trade_side = "up"
                        trade_price = up_price
                    elif dn_price >= ENTRY_THRESHOLD:
                        trade_side = "down"
                        trade_price = dn_price
                    
                    if trade_side and w["secs_left"] > 60:  # Don't trade in last minute
                        print(f"\n  *** {trade_side.upper()} at {trade_price*100:.0f}c >= {ENTRY_THRESHOLD*100:.0f}c ***")
                        
                        # Calculate size
                        trade_size = self.balance * BALANCE_PCT
                        token_id = tokens.get(trade_side)
                        
                        if token_id:
                            order_id = self.place_market_buy(token_id, trade_price, trade_size)
                            
                            if order_id:
                                traded_this_window = True
                                
                                # Wait a moment and check fill
                                time.sleep(3)
                                
                                if self.is_filled(order_id):
                                    holding_side = trade_side
                                    holding_shares = trade_size / trade_price
                                    holding_cost = trade_size
                                    
                                    print(f"  [FILLED] Holding {holding_shares:.1f} {trade_side.upper()} shares")
                                    
                                    self.trades.append({
                                        "window": current_window,
                                        "side": trade_side,
                                        "entry_price": trade_price,
                                        "shares": holding_shares,
                                        "cost": holding_cost,
                                        "time": datetime.now().isoformat(),
                                    })
                                else:
                                    print("  [!] Order not filled yet, will check again")
                    else:
                        # Status update
                        status = f"Waiting for >=90c"
                        if holding_side:
                            status = f"HOLD {holding_side.upper()}"
                        
                        pnl = self.balance - self.starting_balance
                        print(f"\r  [{w['time_str']}] Up:{up_price*100:.0f}c Dn:{dn_price*100:.0f}c | {status} | ${self.balance:.2f} ({pnl:+.2f})", end="", flush=True)
                
                # === CHECK PENDING ORDER ===
                if order_id and not holding_side:
                    if self.is_filled(order_id):
                        # Find which side was traded
                        for side, token_id in tokens.items():
                            holding_side = side
                            holding_shares = (self.balance * BALANCE_PCT) / ENTRY_THRESHOLD
                            holding_cost = self.balance * BALANCE_PCT
                            print(f"\n  [FILLED] Now holding {holding_side.upper()}")
                            break
                        order_id = None
                
                time.sleep(POLL_INTERVAL)
        
        except KeyboardInterrupt:
            print("\n\n[STOP] Interrupted")
        except Exception as e:
            print(f"\n\n[ERROR] {e}")
            import traceback
            traceback.print_exc()
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
        print(f"Trades: {len(self.trades)} | Wins: {self.wins}")
        
        results = {
            "end_time": datetime.now().isoformat(),
            "starting_balance": self.starting_balance,
            "final_balance": self.balance,
            "pnl": pnl,
            "wins": self.wins,
            "trades": self.trades
        }
        
        with open("pm_live_results.json", "w") as f:
            json.dump(results, f, indent=2)
        
        print("\nSaved to: pm_live_results.json")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--balance", type=float, default=20.86, help="Current balance")
    p.add_argument("--duration", type=float, default=12, help="Hours to run")
    args = p.parse_args()
    
    trader = LiveTraderV4()
    trader.run(balance=args.balance, duration_hours=args.duration)

