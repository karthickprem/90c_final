"""
WHALE COPY TRADING BOT for Polymarket

Monitors large trades and copies them.

Strategy:
1. Watch for trades above threshold (e.g., $5k+)
2. When a whale buys, buy the same outcome
3. When whale sells or exits, exit

Sources of whale data:
- Polymarket API (if trade history available)
- On-chain transaction monitoring
- @PolywhalesALERT Twitter feed
"""

import requests
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()


@dataclass
class WhaleTrade:
    """A detected whale trade."""
    timestamp: float
    market_slug: str
    outcome: str
    side: str  # "buy" or "sell"
    size_usd: float
    price: float
    wallet: str = ""


@dataclass
class CopiedPosition:
    """A position we copied from a whale."""
    whale_trade: WhaleTrade
    our_entry_price: float
    our_size: float
    status: str = "open"  # open, closed
    exit_price: Optional[float] = None
    pnl: Optional[float] = None


class WhaleCopyBot:
    """
    Copies trades from whale wallets.
    """
    
    def __init__(
        self,
        min_whale_size: float = 5000,  # Minimum trade to copy
        copy_size: float = 50,  # Our position size
        max_positions: int = 10,
        output_dir: str = "whale_results",
        paper_mode: bool = True,
    ):
        self.min_whale_size = min_whale_size
        self.copy_size = copy_size
        self.max_positions = max_positions
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.paper_mode = paper_mode
        
        # Known whale wallets (add from @PolywhalesALERT or your research)
        self.whale_wallets = [
            # Add whale wallet addresses here
            # These can be found from:
            # - @PolywhalesALERT Twitter
            # - Polymarket leaderboard
            # - On-chain analysis
        ]
        
        # State
        self.whale_trades: List[WhaleTrade] = []
        self.positions: List[CopiedPosition] = []
        self.alerts: List[str] = []
        
        # Log
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"whale_copy_{ts_str}.jsonl"
    
    def _log(self, event: str, data: dict):
        with open(self.log_path, "a", encoding="utf-8") as f:
            record = {"ts": time.time(), "event": event, **data}
            f.write(json.dumps(record) + "\n")
    
    def fetch_market_activity(self, slug: str) -> List[dict]:
        """
        Fetch recent activity for a market.
        
        Note: Polymarket's public API may not expose full trade history.
        For real implementation, you'd need:
        - WebSocket connection to trade stream
        - On-chain event monitoring (Polygon)
        - Third-party data provider
        """
        # Try to get market data
        try:
            r = session.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)
            markets = r.json()
            if markets:
                return markets
        except:
            pass
        return []
    
    def check_polywhales_twitter(self):
        """
        Parse @PolywhalesALERT for whale trades.
        
        This would require Twitter API access.
        For now, we'll simulate with manual input.
        """
        print("\n" + "=" * 60)
        print("WHALE ALERT MONITOR")
        print("=" * 60)
        print("\nFollow @PolywhalesALERT on Twitter for real-time alerts")
        print("\nExample alert format:")
        print("  '🐋 $50,000 bet on Trump winning at 52c'")
        print("\nWhen you see a whale alert, enter it here:")
    
    def parse_whale_alert(self, alert_text: str) -> Optional[WhaleTrade]:
        """
        Parse a whale alert string into a trade.
        
        Expected format: "$50,000 bet on {outcome} at {price}c"
        """
        try:
            # Very basic parsing - would need improvement for real use
            import re
            
            # Extract amount
            amount_match = re.search(r'\$([0-9,]+)', alert_text)
            if not amount_match:
                return None
            amount = float(amount_match.group(1).replace(',', ''))
            
            # Extract price (cents)
            price_match = re.search(r'at\s+(\d+(?:\.\d+)?)\s*c', alert_text.lower())
            price = float(price_match.group(1)) / 100 if price_match else 0.5
            
            # Determine outcome (simplified)
            outcome = "unknown"
            if "trump" in alert_text.lower():
                outcome = "Trump"
            elif "biden" in alert_text.lower():
                outcome = "Biden"
            elif "yes" in alert_text.lower():
                outcome = "Yes"
            elif "no" in alert_text.lower():
                outcome = "No"
            
            return WhaleTrade(
                timestamp=time.time(),
                market_slug="manual",
                outcome=outcome,
                side="buy",
                size_usd=amount,
                price=price,
            )
        except Exception as e:
            print(f"Parse error: {e}")
            return None
    
    def should_copy(self, trade: WhaleTrade) -> bool:
        """Decide if we should copy this trade."""
        # Check size threshold
        if trade.size_usd < self.min_whale_size:
            return False
        
        # Check position limits
        if len([p for p in self.positions if p.status == "open"]) >= self.max_positions:
            return False
        
        # Check if we already have position in same market/outcome
        for pos in self.positions:
            if pos.status == "open" and pos.whale_trade.outcome == trade.outcome:
                return False
        
        return True
    
    def execute_copy(self, trade: WhaleTrade):
        """Execute a copy trade (paper or live)."""
        if not self.should_copy(trade):
            print(f"Skipping trade: {trade.outcome} (limits/duplicate)")
            return
        
        # Get current price
        # In real implementation, fetch actual orderbook
        current_price = trade.price  # Assume we get same price
        
        position = CopiedPosition(
            whale_trade=trade,
            our_entry_price=current_price,
            our_size=self.copy_size / current_price,  # Shares
        )
        
        self.positions.append(position)
        
        mode = "PAPER" if self.paper_mode else "LIVE"
        print(f"\n[{mode}] COPIED TRADE:")
        print(f"  Whale: ${trade.size_usd:,.0f} on {trade.outcome} @ {trade.price:.2f}")
        print(f"  Us: ${self.copy_size:.0f} on {trade.outcome} @ {current_price:.2f}")
        print(f"  Shares: {position.our_size:.1f}")
        
        self._log("COPY_TRADE", {
            "whale_size": trade.size_usd,
            "outcome": trade.outcome,
            "whale_price": trade.price,
            "our_price": current_price,
            "our_size": position.our_size,
        })
    
    def show_positions(self):
        """Show current positions."""
        open_positions = [p for p in self.positions if p.status == "open"]
        
        if not open_positions:
            print("\nNo open positions")
            return
        
        print(f"\nOpen positions: {len(open_positions)}")
        for i, pos in enumerate(open_positions, 1):
            cost = pos.our_entry_price * pos.our_size
            print(f"  {i}. {pos.whale_trade.outcome} @ {pos.our_entry_price:.2f}")
            print(f"     Shares: {pos.our_size:.1f}, Cost: ${cost:.2f}")
            print(f"     Whale size: ${pos.whale_trade.size_usd:,.0f}")
    
    def run_interactive(self):
        """
        Run in interactive mode - manually enter whale alerts.
        """
        print("\n" + "=" * 70)
        print("WHALE COPY BOT - INTERACTIVE MODE")
        print("=" * 70)
        print(f"\nSettings:")
        print(f"  Min whale size: ${self.min_whale_size:,.0f}")
        print(f"  Our copy size: ${self.copy_size:.0f}")
        print(f"  Max positions: {self.max_positions}")
        print(f"  Mode: {'PAPER' if self.paper_mode else 'LIVE'}")
        
        print("\n" + "-" * 70)
        print("COMMANDS:")
        print("  Enter whale alert text to copy")
        print("  'show' - Show positions")
        print("  'quit' - Exit")
        print("-" * 70)
        
        while True:
            try:
                user_input = input("\n> ").strip()
                
                if not user_input:
                    continue
                
                if user_input.lower() == 'quit':
                    break
                
                if user_input.lower() == 'show':
                    self.show_positions()
                    continue
                
                # Try to parse as whale alert
                trade = self.parse_whale_alert(user_input)
                if trade:
                    print(f"\nParsed: ${trade.size_usd:,.0f} on {trade.outcome} @ {trade.price:.2f}")
                    if trade.size_usd >= self.min_whale_size:
                        self.execute_copy(trade)
                    else:
                        print(f"Trade too small (min: ${self.min_whale_size:,.0f})")
                else:
                    print("Could not parse alert. Format: '$50,000 bet on Trump at 52c'")
            
            except KeyboardInterrupt:
                break
        
        print("\nExiting whale copy bot")
        self.show_positions()


class AutoWhaleMonitor:
    """
    Automatic whale monitoring via on-chain data.
    
    This is a more advanced implementation that would:
    1. Connect to Polygon blockchain
    2. Monitor Polymarket contract events
    3. Detect large trades in real-time
    
    Requires:
    - Web3 connection to Polygon
    - Polymarket contract ABIs
    - Real-time event subscription
    """
    
    def __init__(self):
        self.polygon_rpc = "https://polygon-rpc.com"  # Public RPC
        # Polymarket contract addresses would go here
    
    def explain_setup(self):
        print("\n" + "=" * 70)
        print("AUTOMATIC WHALE MONITORING")
        print("=" * 70)
        print("""
To automatically monitor whale trades, you need:

1. POLYGON RPC CONNECTION
   - Use Alchemy, Infura, or public RPC
   - Subscribe to Polymarket contract events

2. POLYMARKET CONTRACT ADDRESSES
   - CTF Exchange: 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
   - Monitor 'OrderFilled' events

3. DECODE TRADE DATA
   - Parse event logs to extract:
     - Wallet address
     - Token ID (outcome)
     - Size and price

4. FILTER FOR WHALES
   - Track top wallets
   - Alert on trades > threshold

ALTERNATIVE: Use @PolywhalesALERT Twitter feed
   - Already does the monitoring
   - Just need to react to their alerts
   - Can use Twitter API or manual copying
""")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Whale Copy Bot")
    parser.add_argument("--interactive", action="store_true", help="Run interactive mode")
    parser.add_argument("--explain", action="store_true", help="Explain auto monitoring")
    parser.add_argument("--min-size", type=float, default=5000, help="Min whale trade size")
    parser.add_argument("--copy-size", type=float, default=50, help="Our copy size")
    
    args = parser.parse_args()
    
    if args.explain:
        monitor = AutoWhaleMonitor()
        monitor.explain_setup()
    elif args.interactive:
        bot = WhaleCopyBot(
            min_whale_size=args.min_size,
            copy_size=args.copy_size,
        )
        bot.run_interactive()
    else:
        print("Whale Copy Bot")
        print("\nUsage:")
        print("  Interactive: python whale_copy_bot.py --interactive")
        print("  Explain auto: python whale_copy_bot.py --explain")
        
        # Show sample alert parsing
        print("\n\nSample alert parsing:")
        bot = WhaleCopyBot()
        samples = [
            "$50,000 bet on Trump at 52c",
            "$25,000 on Yes at 85c",
            "Whale drops $100,000 on Biden at 48c",
        ]
        for sample in samples:
            trade = bot.parse_whale_alert(sample)
            if trade:
                print(f"  '{sample}'")
                print(f"    -> ${trade.size_usd:,.0f} on {trade.outcome} @ {trade.price:.2f}")


if __name__ == "__main__":
    main()

