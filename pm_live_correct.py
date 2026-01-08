"""
POLYMARKET LIVE TRADER - CORRECT VERSION
=========================================
- Reads REAL USDC balance from blockchain
- Uses 90% of balance, split by 2
- Places orders for BOTH Up and Down at 90c
- When one fills, cancel the other
- Let position settle at $1
- Repeat with updated balance
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

ENTRY_PRICE = 0.90
BALANCE_PCT = 0.90  # Use 90% of balance
MIN_SHARES = 5
ORDER_DELAY = 3.0
POLL_INTERVAL = 5
SETTLE_WAIT = 90  # Wait for settlement


class LiveTraderCorrect:
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
        
        # Web3 for reading balance
        self.w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        self.usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        self.usdc_abi = [{"constant":True,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
        self.usdc_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.usdc_address), 
            abi=self.usdc_abi
        )
        
        self.session = requests.Session()
        self.trades = []
        self.wins = 0
        
        print("=" * 60)
        print("POLYMARKET LIVE TRADER - CORRECT VERSION")
        print("=" * 60)
        print(f"Proxy: {self.proxy_address}")
        print("Strategy:")
        print("  1. Read REAL USDC balance from blockchain")
        print("  2. Use 90% of balance, split by 2")
        print("  3. Place orders for BOTH Up and Down at 90c")
        print("  4. When one fills, cancel the other")
        print("  5. Let position settle at $1")
        print("=" * 60)
    
    def get_usdc_balance(self) -> float:
        """Read actual USDC balance from blockchain"""
        try:
            balance = self.usdc_contract.functions.balanceOf(
                Web3.to_checksum_address(self.proxy_address)
            ).call()
            return balance / 1e6  # USDC has 6 decimals
        except Exception as e:
            print(f"  [!] Balance error: {e}")
            return 0
    
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
    
    def get_prices(self, tokens: Dict) -> Dict:
        prices = {}
        for side, token_id in tokens.items():
            try:
                r = self.session.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=5)
                prices[side] = float(r.json().get("mid", 0))
            except:
                prices[side] = 0
        return prices
    
    def place_order(self, token_id: str, side_name: str, amount_usd: float) -> Optional[str]:
        try:
            shares = amount_usd / ENTRY_PRICE
            if shares < MIN_SHARES:
                print(f"    [{side_name.upper()}] Skipping - {shares:.1f} shares < minimum {MIN_SHARES}")
                return None
            
            print(f"    [{side_name.upper()}] {shares:.1f} shares @ {ENTRY_PRICE*100:.0f}c = ${shares*ENTRY_PRICE:.2f}")
            
            order_args = OrderArgs(
                token_id=token_id,
                price=ENTRY_PRICE,
                size=shares,
                side=BUY,
            )
            
            signed = self.client.create_order(order_args)
            result = self.client.post_order(signed, OrderType.GTC)
            
            if result and result.get("success"):
                order_id = result.get("orderID")
                print(f"    [OK] {order_id[:30]}...")
                return order_id
            else:
                print(f"    [FAIL] {result}")
                
        except Exception as e:
            err = str(e)
            if "not enough balance" in err:
                print(f"    [{side_name.upper()}] Insufficient balance")
            else:
                print(f"    [ERROR] {e}")
        
        return None
    
    def cancel_order(self, order_id: str):
        try:
            self.client.cancel(order_id)
        except:
            pass
    
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
                if status in ["MATCHED", "FILLED", "CLOSED"]:
                    return True
                size_matched = float(order.get("size_matched", 0))
                original_size = float(order.get("original_size", 1))
                if size_matched >= original_size * 0.5:
                    return True
        except:
            pass
        return False
    
    def run(self, duration_hours: float = 12):
        # Get initial balance
        balance = self.get_usdc_balance()
        print(f"\n>>> Initial USDC balance: ${balance:.2f}")
        
        if balance < MIN_SHARES * ENTRY_PRICE:
            print(f"[!] Balance too low. Need at least ${MIN_SHARES * ENTRY_PRICE:.2f}")
            return
        
        print(f">>> Duration: {duration_hours}h")
        print("=" * 60)
        
        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        starting_balance = balance
        
        current_window = None
        tokens = {}
        orders = {}
        holding = None
        holding_shares = 0
        holding_cost = 0
        
        try:
            while time.time() < deadline:
                w = self.get_window()
                elapsed = (time.time() - start_time) / 60
                
                # === NEW WINDOW ===
                if w["slug"] != current_window:
                    # Settlement from previous window
                    if holding:
                        print(f"\n  [SETTLE] Window closed, waiting {SETTLE_WAIT}s...")
                        time.sleep(SETTLE_WAIT)
                        
                        # Get new balance from blockchain
                        new_balance = self.get_usdc_balance()
                        profit = new_balance - (balance - holding_cost)
                        
                        if profit > 0:
                            self.wins += 1
                            print(f"  [WIN] Profit: +${profit:.2f}")
                        else:
                            print(f"  [LOSS] ${profit:.2f}")
                        
                        balance = new_balance
                        print(f"  [BALANCE] ${balance:.2f}")
                        
                        holding = None
                        holding_shares = 0
                        holding_cost = 0
                    
                    self.cancel_all()
                    
                    # Read fresh balance
                    balance = self.get_usdc_balance()
                    
                    print(f"\n[{elapsed:.1f}m] NEW WINDOW: {w['slug']}")
                    print(f"  USDC Balance: ${balance:.2f}")
                    
                    current_window = w["slug"]
                    tokens = self.get_tokens(current_window)
                    orders = {}
                    
                    if not tokens:
                        print("  No tokens found")
                        time.sleep(10)
                        continue
                    
                    if balance < MIN_SHARES * ENTRY_PRICE * 2:
                        print(f"  [!] Balance too low for both sides")
                        time.sleep(60)
                        continue
                    
                    # === PLACE ORDERS: 90% balance, split by 2 ===
                    usable = balance * BALANCE_PCT
                    per_side = usable / 2
                    
                    print(f"  Using 90%: ${usable:.2f} -> ${per_side:.2f} per side")
                    
                    # Place UP order
                    up_oid = self.place_order(tokens["up"], "up", per_side)
                    if up_oid:
                        orders["up"] = {"id": up_oid, "cost": per_side}
                    
                    time.sleep(ORDER_DELAY)
                    
                    # Place DOWN order
                    down_oid = self.place_order(tokens["down"], "down", per_side)
                    if down_oid:
                        orders["down"] = {"id": down_oid, "cost": per_side}
                    
                    if not orders:
                        print("  [!] No orders placed")
                
                # === CHECK FOR FILLS ===
                if orders and not holding:
                    for side, info in list(orders.items()):
                        if self.is_filled(info["id"]):
                            print(f"\n  *** {side.upper()} FILLED! ***")
                            holding = side
                            holding_cost = info["cost"]
                            holding_shares = holding_cost / ENTRY_PRICE
                            
                            # Cancel other side
                            other = "down" if side == "up" else "up"
                            if other in orders:
                                print(f"  Cancelling {other.upper()}...")
                                self.cancel_order(orders[other]["id"])
                            
                            orders = {}
                            
                            self.trades.append({
                                "window": current_window,
                                "side": side,
                                "shares": holding_shares,
                                "cost": holding_cost,
                                "time": datetime.now().isoformat()
                            })
                            break
                
                # === STATUS ===
                if tokens:
                    prices = self.get_prices(tokens)
                    up = prices.get("up", 0)
                    dn = prices.get("down", 0)
                    
                    if holding:
                        status = f"HOLD {holding.upper()}"
                    elif orders:
                        status = f"ORDERS: {list(orders.keys())}"
                    else:
                        status = "IDLE"
                    
                    pnl = balance - starting_balance
                    print(f"\r  [{w['time_str']}] Up:{up*100:.0f}c Dn:{dn*100:.0f}c | {status} | ${balance:.2f} ({pnl:+.2f})", end="", flush=True)
                
                time.sleep(POLL_INTERVAL)
        
        except KeyboardInterrupt:
            print("\n\n[STOP]")
        except Exception as e:
            print(f"\n\n[ERROR] {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.cancel_all()
            self.summary(starting_balance)
    
    def summary(self, starting_balance):
        balance = self.get_usdc_balance()
        pnl = balance - starting_balance
        
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Starting: ${starting_balance:.2f}")
        print(f"Final:    ${balance:.2f}")
        print(f"P&L:      ${pnl:+.2f}")
        print(f"Trades:   {len(self.trades)}")
        print(f"Wins:     {self.wins}")
        
        with open("pm_live_results.json", "w") as f:
            json.dump({
                "starting": starting_balance,
                "final": balance,
                "pnl": pnl,
                "trades": self.trades,
                "wins": self.wins
            }, f, indent=2)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=12, help="Hours to run")
    args = p.parse_args()
    
    trader = LiveTraderCorrect()
    trader.run(duration_hours=args.duration)
