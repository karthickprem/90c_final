"""
POLYMARKET LIVE TRADER v5 - READS REAL BALANCE
===============================================
- Fetches actual balance from Polymarket
- No manual balance calculation
- Conservative trade sizing
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
TRADE_SIZE_USD = 5.0    # Fixed $5 per trade (conservative)
MIN_SHARES = 5
POLL_INTERVAL = 5
SETTLE_WAIT = 60        # Wait longer for settlement


class LiveTraderV5:
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
        
        # State
        self.trades = []
        self.wins = 0
        
        print("=" * 60)
        print("POLYMARKET LIVE TRADER v5")
        print("=" * 60)
        print(f"Proxy: {self.proxy_address}")
        print(f"Strategy: BUY when price >= {ENTRY_THRESHOLD*100:.0f}c")
        print(f"Trade size: ${TRADE_SIZE_USD:.2f} per trade (fixed)")
        print("=" * 60)
    
    def get_balance(self) -> float:
        """Get actual USDC balance from Polymarket"""
        try:
            # Try to get positions/balance info
            # The balance is in the proxy wallet
            url = f"https://data-api.polymarket.com/value?user={self.proxy_address}"
            r = self.session.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                # This returns portfolio value
                return float(data.get("value", 0))
        except Exception as e:
            print(f"  Balance API error: {e}")
        
        # Fallback: try gamma API
        try:
            url = f"{GAMMA_API}/users/{self.proxy_address}"
            r = self.session.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                return float(data.get("balance", 0))
        except:
            pass
        
        return 0
    
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
    
    def place_order(self, token_id: str, price: float) -> Optional[str]:
        """Place a fixed-size order"""
        try:
            shares = TRADE_SIZE_USD / price
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
                print(f"    [!] Failed: {result}")
                
        except Exception as e:
            err_str = str(e)
            if "not enough balance" in err_str:
                print(f"    [!] Insufficient balance - skipping this window")
            else:
                print(f"    [X] Error: {e}")
        
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
    
    def run(self, duration_hours: float = 12):
        # Get initial balance
        balance = self.get_balance()
        print(f"\n>>> Initial balance from Polymarket: ${balance:.2f}")
        
        if balance < TRADE_SIZE_USD:
            print(f"[!] Balance too low for ${TRADE_SIZE_USD} trades")
            print("    Reduce TRADE_SIZE_USD or add funds")
        
        print(f">>> Duration: {duration_hours} hours")
        print("=" * 60)
        
        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        
        current_window = None
        tokens = {}
        order_id = None
        holding_side = None
        holding_cost = 0
        traded_this_window = False
        last_balance_check = 0
        
        try:
            while time.time() < deadline:
                w = self.get_window()
                elapsed = (time.time() - start_time) / 60
                
                # Check balance every 5 minutes
                if time.time() - last_balance_check > 300:
                    balance = self.get_balance()
                    last_balance_check = time.time()
                    print(f"\n  [BALANCE CHECK] ${balance:.2f}")
                
                # === NEW WINDOW ===
                if w["slug"] != current_window:
                    # If we were holding, settlement happened
                    if holding_side:
                        print(f"\n  [SETTLE] Waiting {SETTLE_WAIT}s for settlement...")
                        time.sleep(SETTLE_WAIT)
                        
                        # Get actual balance after settlement
                        new_balance = self.get_balance()
                        profit = new_balance - balance
                        balance = new_balance
                        
                        if profit > 0:
                            self.wins += 1
                            print(f"  [SETTLE] WIN: +${profit:.2f}")
                        else:
                            print(f"  [SETTLE] LOSS: ${profit:.2f}")
                        
                        print(f"  [SETTLE] Balance: ${balance:.2f}")
                        
                        holding_side = None
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
                
                # === WAIT FOR PRICE >= 90c ===
                if not traded_this_window and not holding_side and tokens:
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
                    
                    if trade_side and w["secs_left"] > 60:
                        print(f"\n  *** {trade_side.upper()} at {trade_price*100:.0f}c ***")
                        
                        token_id = tokens.get(trade_side)
                        if token_id:
                            order_id = self.place_order(token_id, trade_price)
                            
                            if order_id:
                                traded_this_window = True
                                time.sleep(3)
                                
                                if self.is_filled(order_id):
                                    holding_side = trade_side
                                    holding_cost = TRADE_SIZE_USD
                                    print(f"  [FILLED] Holding {trade_side.upper()}")
                                    
                                    self.trades.append({
                                        "window": current_window,
                                        "side": trade_side,
                                        "price": trade_price,
                                        "cost": holding_cost,
                                        "time": datetime.now().isoformat(),
                                    })
                            else:
                                # Order failed, don't retry this window
                                traded_this_window = True
                    else:
                        # Status
                        status = "Waiting for >=90c"
                        if holding_side:
                            status = f"HOLD {holding_side.upper()}"
                        
                        print(f"\r  [{w['time_str']}] Up:{up_price*100:.0f}c Dn:{dn_price*100:.0f}c | {status} | ${balance:.2f}", end="", flush=True)
                
                # === CHECK ORDER FILL ===
                if order_id and not holding_side:
                    if self.is_filled(order_id):
                        holding_side = "unknown"
                        holding_cost = TRADE_SIZE_USD
                        print(f"\n  [FILLED]")
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
        print("SUMMARY")
        print("=" * 60)
        
        final_balance = self.get_balance()
        print(f"Final balance: ${final_balance:.2f}")
        print(f"Trades: {len(self.trades)} | Wins: {self.wins}")
        
        results = {
            "end_time": datetime.now().isoformat(),
            "final_balance": final_balance,
            "wins": self.wins,
            "trades": self.trades
        }
        
        with open("pm_live_results.json", "w") as f:
            json.dump(results, f, indent=2)
        
        print("Saved: pm_live_results.json")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=12, help="Hours")
    p.add_argument("--size", type=float, default=5.0, help="USD per trade")
    args = p.parse_args()
    
    TRADE_SIZE_USD = args.size
    
    trader = LiveTraderV5()
    trader.run(duration_hours=args.duration)

