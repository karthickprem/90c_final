"""
POLYMARKET LIVE BOT - Simple & Complete
Strategy: Buy when Up or Down price >= 90c, settle at $1
"""

import requests
import json
import time
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds
from py_clob_client.order_builder.constants import BUY
from py_clob_client.constants import POLYGON

# APIs
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# USDC contract on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_ABI = [{"inputs":[{"name":"account","type":"address"}],
             "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]

# Config
ENTRY_THRESHOLD = 0.90  # 90c
BALANCE_PCT = 0.90      # Use 90% of balance
MIN_TIME_SECS = 30      # Don't trade in last 30 seconds
POLL_MS = 200           # Poll every 200ms

session = requests.Session()


def load_config():
    with open("pm_api_config.json") as f:
        return json.load(f)


def get_window():
    ts = int(time.time())
    start = ts - (ts % 900)
    end = start + 900
    secs_left = end - ts
    return {
        "slug": f"btc-updown-15m-{start}",
        "secs_left": secs_left,
        "time_str": f"{secs_left // 60}:{secs_left % 60:02d}"
    }


def get_tokens(slug):
    try:
        r = session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        markets = r.json()
        if markets:
            m = markets[0]
            tokens = m.get("clobTokenIds", [])
            outcomes = m.get("outcomes", [])
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            return {o.lower(): t for o, t in zip(outcomes, tokens)}
    except Exception as e:
        print(f"  Token error: {e}")
    return {}


def get_prices(tokens):
    prices = {}
    for side, token in tokens.items():
        try:
            r = session.get(f"{CLOB_API}/midpoint", params={"token_id": token}, timeout=3)
            if r.status_code == 200:
                prices[side] = float(r.json().get("mid", 0))
            else:
                prices[side] = 0
        except:
            prices[side] = 0
    return prices


def get_outcome(slug):
    """Check if window is resolved and get winner."""
    try:
        r = session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        markets = r.json()
        if markets:
            m = markets[0]
            if not m.get("closed"):
                return None
            
            prices = m.get("outcomePrices", [])
            outcomes = m.get("outcomes", [])
            if isinstance(prices, str):
                prices = json.loads(prices)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            for o, p in zip(outcomes, prices):
                if float(p) >= 0.99:
                    return o.lower()
    except:
        pass
    return None


class LiveBot:
    def __init__(self):
        config = load_config()
        
        creds = ApiCreds(
            api_key=config["api_key"],
            api_secret=config["api_secret"],
            api_passphrase=config["api_passphrase"]
        )
        
        self.client = ClobClient(
            host=CLOB_API,
            key=config["private_key"],
            chain_id=POLYGON,
            creds=creds,
            signature_type=1,
            funder=config["proxy_address"]
        )
        
        self.proxy = config["proxy_address"]
        
        # Web3 for reading balance from blockchain
        self.w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        self.usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS), 
            abi=USDC_ABI
        )
        
        # State
        self.tokens = {}
        self.current_slug = None
        self.traded_this_window = False
        self.holding_side = None
        self.holding_price = 0
        self.holding_shares = 0
        self.holding_cost = 0
        
        # Stats
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0
        self.starting_balance = 0
    
    def get_balance(self):
        """Get USDC balance from blockchain."""
        try:
            bal = self.usdc.functions.balanceOf(
                Web3.to_checksum_address(self.proxy)
            ).call()
            return bal / 1e6  # USDC has 6 decimals
        except:
            return 0
    
    def place_order(self, side, token, price, size):
        """Place a buy order."""
        try:
            from py_clob_client.order_builder.constants import BUY
            args = OrderArgs(token_id=token, price=price, size=size, side=BUY)
            signed = self.client.create_order(args)
            result = self.client.post_order(signed, OrderType.GTC)
            
            if result and result.get("success"):
                order_id = result.get("orderID")
                print(f"  ORDER OK: {order_id[:40]}...")
                
                # Wait and get fill details
                time.sleep(1)
                try:
                    order = self.client.get_order(order_id)
                    if order:
                        filled = float(order.get("size_matched", 0))
                        fill_price = float(order.get("price", price))
                        if filled > 0:
                            return {
                                "filled": True,
                                "shares": filled,
                                "price": fill_price,
                                "cost": filled * fill_price
                            }
                except:
                    pass
                
                # Assume fill at our price
                return {
                    "filled": True,
                    "shares": size,
                    "price": price,
                    "cost": size * price
                }
            else:
                print(f"  ORDER FAILED: {result}")
        except Exception as e:
            print(f"  ORDER ERROR: {e}")
        
        return {"filled": False}
    
    def cancel_all(self):
        """Cancel all open orders."""
        try:
            self.client.cancel_all()
        except:
            pass
    
    def run(self, duration_hours=12):
        print("=" * 60)
        print("POLYMARKET LIVE BOT")
        print("=" * 60)
        print(f"Proxy: {self.proxy}")
        print(f"Strategy: Buy when price >= {int(ENTRY_THRESHOLD*100)}c")
        print(f"Balance: {int(BALANCE_PCT*100)}% per trade")
        print("=" * 60)
        
        self.starting_balance = self.get_balance()
        print(f"Starting balance: ${self.starting_balance:.2f}")
        print("=" * 60)
        
        start_time = time.time()
        deadline = start_time + duration_hours * 3600
        pending_settlement = None
        
        try:
            while time.time() < deadline:
                w = get_window()
                
                # New window?
                if w["slug"] != self.current_slug:
                    # Settle previous window
                    if pending_settlement and self.holding_side:
                        self.settle(pending_settlement)
                    
                    # Setup new window
                    print(f"\n[NEW WINDOW] {w['slug']}")
                    self.cancel_all()
                    pending_settlement = self.current_slug
                    self.current_slug = w["slug"]
                    self.tokens = get_tokens(self.current_slug)
                    self.traded_this_window = False
                    
                    balance = self.get_balance()
                    print(f"  Balance: ${balance:.2f}")
                
                # Get prices
                if self.tokens:
                    prices = get_prices(self.tokens)
                    up = prices.get("up", 0)
                    down = prices.get("down", 0)
                    
                    # Entry logic
                    if not self.traded_this_window and not self.holding_side:
                        if w["secs_left"] >= MIN_TIME_SECS:
                            
                            if up >= ENTRY_THRESHOLD:
                                print(f"\n  *** SIGNAL: UP @ {int(up*100)}c ***")
                                self.enter_trade("up", up)
                            
                            elif down >= ENTRY_THRESHOLD:
                                print(f"\n  *** SIGNAL: DOWN @ {int(down*100)}c ***")
                                self.enter_trade("down", down)
                    
                    # Status display
                    hold_str = ""
                    if self.holding_side:
                        hold_str = f" | HOLD {self.holding_side.upper()}"
                    
                    pnl = self.get_balance() - self.starting_balance
                    print(f"\r  [{w['time_str']}] Up: {int(up*100)}c | Down: {int(down*100)}c{hold_str} | W{self.wins}/L{self.losses} | PnL: ${pnl:+.2f}  ", end="", flush=True)
                
                time.sleep(POLL_MS / 1000)
        
        except KeyboardInterrupt:
            print("\n\nStopped by user")
        
        finally:
            self.cancel_all()
            self.summary()
    
    def enter_trade(self, side, price):
        """Enter a trade."""
        balance = self.get_balance()
        use_amount = balance * BALANCE_PCT
        shares = use_amount / price
        
        if shares < 5:
            print(f"  Not enough balance (need 5+ shares)")
            return
        
        print(f"  Buying {shares:.1f} shares @ {int(price*100)}c = ${use_amount:.2f}")
        
        token = self.tokens.get(side)
        if not token:
            print(f"  No token for {side}")
            return
        
        result = self.place_order(side, token, price, shares)
        
        if result.get("filled"):
            self.holding_side = side
            self.holding_price = result["price"]
            self.holding_shares = result["shares"]
            self.holding_cost = result["cost"]
            self.traded_this_window = True
            print(f"  FILLED: {result['shares']:.1f} @ {int(result['price']*100)}c = ${result['cost']:.2f}")
        else:
            print(f"  ORDER NOT FILLED")
    
    def settle(self, slug):
        """Settle a completed window."""
        if not self.holding_side:
            return
        
        print(f"\n\n{'='*60}")
        print(f"SETTLING: {slug}")
        print(f"{'='*60}")
        print(f"  Position: {self.holding_side.upper()} @ {int(self.holding_price*100)}c")
        print(f"  Shares: {self.holding_shares:.1f}")
        print(f"  Cost: ${self.holding_cost:.2f}")
        
        # Wait for outcome
        print(f"  Waiting for outcome...")
        winner = None
        for i in range(60):  # Wait up to 60 seconds
            winner = get_outcome(slug)
            if winner:
                break
            time.sleep(2)
            if i % 5 == 0:
                print(f"  [{i*2}s] Checking...")
        
        if winner:
            print(f"  Winner: {winner.upper()}")
            
            if winner == self.holding_side:
                # WIN - get $1 per share
                payout = self.holding_shares
                profit = payout - self.holding_cost
                self.total_pnl += profit
                self.wins += 1
                print(f"\n  >>> WIN! <<<")
                print(f"  Payout: ${payout:.2f}")
                print(f"  Profit: ${profit:+.2f}")
            else:
                # LOSS - get $0
                loss = -self.holding_cost
                self.total_pnl += loss
                self.losses += 1
                print(f"\n  >>> LOSS <<<")
                print(f"  Lost: ${loss:.2f}")
        else:
            print(f"  Could not determine winner")
        
        # Clear position
        self.holding_side = None
        self.holding_price = 0
        self.holding_shares = 0
        self.holding_cost = 0
        
        print(f"{'='*60}")
    
    def summary(self):
        balance = self.get_balance()
        pnl = balance - self.starting_balance
        
        print(f"\n\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")
        print(f"Starting: ${self.starting_balance:.2f}")
        print(f"Final:    ${balance:.2f}")
        print(f"P&L:      ${pnl:+.2f}")
        print(f"Wins:     {self.wins}")
        print(f"Losses:   {self.losses}")
        if self.wins + self.losses > 0:
            print(f"Win Rate: {self.wins/(self.wins+self.losses)*100:.0f}%")
        print(f"{'='*60}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=12, help="Hours to run")
    args = p.parse_args()
    
    bot = LiveBot()
    bot.run(duration_hours=args.duration)
