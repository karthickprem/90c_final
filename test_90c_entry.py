"""
Test 90c Trading Flow - DUAL STATE MACHINE Version (OPTIMIZED)
===============================================================
Implements the EXACT backtest logic with independent UP/DOWN state machines.

OPTIMIZED PARAMETERS (from 50-day backtest):
  - Entry: 90c in LAST 6 MINUTES only
  - Take Profit: 98c
  - Stop Loss: 55c (tighter SL = higher win rate)
  - Position Size: 5% of balance per trade

STATE MACHINE (per side - UP and DOWN are INDEPENDENT):
  - IDLE: No position, TP not hit → Can enter if price >= 90c AND in last 6 min
  - IN TRADE: Position active → Wait for TP (98c) or SL (55c)
  - DONE: TP or SL hit → Locked out for rest of window ⛔

KEY RULES:
  ✅ Entry only in last 6 minutes of window
  ❌ After SL: Locked out for rest of window
  ❌ After TP: Locked out for rest of window
  ✅ UP and DOWN trade independently (can both be active!)
  ❌ No pyramiding (one position per side max)

FLOW:
  1. WebSocket reads UP/DOWN prices continuously
  2. Wait until last 6 min of window (360s remaining)
  3. If UP at 90c AND up_position=None AND not locked out → Enter UP
  4. If DOWN at 90c AND down_position=None AND not locked out → Enter DOWN
  5. Exit at TP (98c) → Lock out that side
  6. Exit at SL (55c) → Lock out that side
  7. Window end → Close all positions, reset flags

Usage:
    DRY RUN:   python test_90c_entry.py
    LIVE:      python test_90c_entry.py --live
"""

import sys
import os
import asyncio
import json
import time
import math
from datetime import datetime
import requests
import websockets
from dataclasses import dataclass, field
from typing import Optional, Tuple, List

# Excel export
try:
    import pandas as pd
    EXCEL_AVAILABLE = True
except ImportError:
    EXCEL_AVAILABLE = False
    print("[WARNING] pandas not installed - Excel export disabled. Run: pip install pandas openpyxl")

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.constants import POLYGON

# ============================================================================
# CONFIG
# ============================================================================

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Trading thresholds
ENTRY_THRESHOLD = 0.90   # 90c - trigger entry
TP_THRESHOLD = 0.98      # 98c - take profit
SL_THRESHOLD = 0.55      # 55c - stop loss (optimized from backtest)

# Entry timing - only in last 6 minutes of window
ENTRY_WINDOW_SECS = 360  # Last 6 minutes = 360 seconds

# Capital management - 5% per trade
CAPITAL_PERCENTAGE = 0.05  # Use 5% of balance per trade
RUN_DURATION_SECS = 86400  # 24 hours
INITIAL_BALANCE = 100.00   # Starting paper balance ($100)

# Reconnection settings
MAX_RECONNECT_ATTEMPTS = 5   # Max retries within same window
RECONNECT_DELAY_SECS = 3     # Seconds between reconnect attempts

# Check for --live flag (needed early for logging config)
DRY_RUN = "--live" not in sys.argv

# Logging frequency (verbose for paper, quiet for live)
if DRY_RUN:
    LOG_INTERVAL_SECS = 0.1  # 100ms - paper trading (detailed)
else:
    LOG_INTERVAL_SECS = 2.0  # 2 seconds - live trading (clean)

# ============================================================================
# POLYMARKET FEE CALCULATION
# ============================================================================
# Fee structure: Fee = price * (1 - price) * 6.24%
# Max fee is 1.56% at 50% probability (50c)
# Fees decrease toward extremes (near 0c or 100c)
#
# Fee Table (per 100 shares):
#   Price  | Fee    | Effective Rate
#   $0.50  | $0.78  | 1.56%
#   $0.90  | $0.18  | 0.20%
#   $0.95  | $0.05  | 0.06%
#   $0.75  | $0.35  | 0.47%
# ============================================================================

def calculate_polymarket_fee(price: float, size: float = 1.0) -> float:
    """
    Calculate Polymarket taker fee for a trade.
    
    Args:
        price: Trade price (0.01 to 0.99)
        size: Number of shares
    
    Returns:
        Fee in dollars (USDC)
    
    Formula: fee = trade_value * price * (1 - price) * 0.0624
    """
    if price <= 0.01 or price >= 0.99:
        return 0.0
    
    # Effective fee rate = p * (1-p) * 6.24%
    effective_rate = price * (1 - price) * 0.0624
    
    # Trade value = price * size
    trade_value = price * size
    
    # Fee = trade_value * effective_rate
    fee = trade_value * effective_rate
    
    return round(fee, 4)  # Round to 4 decimals (fee precision)


def calculate_fee_cents(price: float) -> float:
    """
    Calculate fee in CENTS per contract (1 share).
    Used for per-contract PnL calculations.
    
    Args:
        price: Trade price (0.01 to 0.99)
    
    Returns:
        Fee in cents per contract
    """
    if price <= 0.01 or price >= 0.99:
        return 0.0
    
    # Fee per contract = price * (1-price) * 0.0624 * 100 cents
    return price * (1 - price) * 0.0624 * 100

# Load API config
API_CONFIG = {}
try:
    with open("pm_api_config.json") as f:
        API_CONFIG = json.load(f)
    print(f"[CONFIG] Loaded API config from pm_api_config.json")
except Exception as e:
    print(f"[CONFIG] Warning: Could not load pm_api_config.json: {e}")

# ============================================================================
# DUAL LOGGER - Terminal + File
# ============================================================================

class DualLogger:
    """Logs to both terminal and file"""
    
    def __init__(self, filename: str):
        self.filename = filename
        self.file = open(filename, "w", encoding="utf-8")
        self._write_header()
    
    def _write_header(self):
        header = f"""
================================================================================
  90c STRATEGY TEST LOG
  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
  Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}
  Duration: {RUN_DURATION_SECS // 60} minutes
================================================================================
"""
        self.file.write(header)
        self.file.flush()
    
    def log(self, msg: str, also_print: bool = True):
        """Log to file and optionally print"""
        timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        line = f"[{timestamp}] {msg}"
        self.file.write(line + "\n")
        self.file.flush()
        if also_print:
            print(msg, flush=True)
    
    def raw(self, msg: str, also_print: bool = True):
        """Log raw message without timestamp"""
        self.file.write(msg + "\n")
        self.file.flush()
        if also_print:
            print(msg, flush=True)
    
    def close(self):
        self.file.close()

# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class BookState:
    token_id: str
    label: str  # "UP" or "DOWN"
    best_bid: float = 0.0
    best_ask: float = 0.0
    mid: float = 0.0
    timestamp: float = 0.0
    
    @property
    def valid(self) -> bool:
        return self.best_bid > 0.01 and self.best_ask < 0.99 and self.best_bid < self.best_ask

@dataclass
class FillRecord:
    """Record of a simulated or real fill - GOLDEN DATA"""
    order_type: str  # "ENTRY", "TP", "SL"
    side: str        # "UP" or "DOWN"
    action: str      # "BUY" or "SELL"
    price: float
    size: float
    timestamp: float
    time_str: str
    bid_at_fill: float
    ask_at_fill: float
    spread_at_fill: float
    secs_into_window: int
    secs_remaining: int
    window_slug: str = ""

@dataclass 
class Position:
    """Active position"""
    token_id: str
    side: str  # "UP" or "DOWN"
    size: float
    entry_price: float
    entry_time: float
    entry_record: FillRecord
    entry_fee: float = 0.0  # Fee paid at entry (in dollars)
    trade_record: Optional['TradeRecord'] = None  # For Excel export
    balance_at_entry: float = 0.0  # Balance before entry (for real PnL calculation)
    buy_order_id: str = ""  # Order ID for tracking MINED status during sell

@dataclass
class WindowStats:
    """Stats per window"""
    slug: str
    start_time: float
    end_time: float
    entries: int = 0
    tp_exits: int = 0
    sl_exits: int = 0
    total_pnl: float = 0.0
    balance_start: float = 0.0
    balance_end: float = 0.0

# ============================================================================
# BALANCE TRACKER
# ============================================================================

class BalanceTracker:
    """Tracks paper trading balance with fees"""
    
    def __init__(self, initial_balance: float, logger: DualLogger):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.logger = logger
        self.trades: List[dict] = []
        self.total_fees_paid = 0.0  # Track cumulative fees
    
    def buy(self, price: float, size: float, fee: float = 0.0) -> float:
        """Record a buy - returns cost (includes fee)"""
        cost = price * size
        total_cost = cost + fee
        self.balance -= total_cost
        self.total_fees_paid += fee
        self.trades.append({
            "action": "BUY",
            "price": price,
            "size": size,
            "cost": cost,
            "fee": fee,
            "total_cost": total_cost,
            "balance_after": self.balance,
            "timestamp": time.time()
        })
        return total_cost
    
    def sell(self, price: float, size: float, fee: float = 0.0) -> float:
        """Record a sell - returns net proceeds (after fee)"""
        gross_proceeds = price * size
        net_proceeds = gross_proceeds - fee
        self.balance += net_proceeds
        self.total_fees_paid += fee
        self.trades.append({
            "action": "SELL",
            "price": price,
            "size": size,
            "gross_proceeds": gross_proceeds,
            "fee": fee,
            "net_proceeds": net_proceeds,
            "balance_after": self.balance,
            "timestamp": time.time()
        })
        return net_proceeds
    
    def get_pnl(self) -> float:
        """Get total PnL in dollars"""
        return self.balance - self.initial_balance
    
    def get_pnl_percent(self) -> float:
        """Get total PnL as percentage"""
        return (self.balance - self.initial_balance) / self.initial_balance * 100
    
    def print_status(self, window_num: int = 0, window_slug: str = ""):
        """Print current balance status"""
        pnl = self.get_pnl()
        pnl_pct = self.get_pnl_percent()
        
        self.logger.raw("")
        self.logger.raw("+" + "-"*78 + "+")
        if window_num > 0:
            self.logger.raw(f"|  WINDOW {window_num} END - {window_slug}")
        self.logger.raw("|" + " "*78 + "|")
        self.logger.raw(f"|  💰 BALANCE SUMMARY (AFTER FEES)")
        self.logger.raw(f"|     Initial:    ${self.initial_balance:>8.2f}")
        self.logger.raw(f"|     Current:    ${self.balance:>8.2f}")
        self.logger.raw(f"|     Fees Paid:  ${self.total_fees_paid:>8.4f}")
        self.logger.raw(f"|     Net PnL:    ${pnl:>+8.2f}  ({pnl_pct:+.1f}%)")
        self.logger.raw("|" + " "*78 + "|")
        self.logger.raw("+" + "-"*78 + "+")
        self.logger.raw("")
    
    def print_final_summary(self):
        """Print final trading summary"""
        pnl = self.get_pnl()
        pnl_pct = self.get_pnl_percent()
        
        buys = [t for t in self.trades if t["action"] == "BUY"]
        sells = [t for t in self.trades if t["action"] == "SELL"]
        
        self.logger.raw("")
        self.logger.raw("=" * 80)
        self.logger.raw("  💰 FINAL BALANCE REPORT (AFTER ALL FEES)")
        self.logger.raw("=" * 80)
        self.logger.raw("")
        self.logger.raw(f"  Starting Balance:   ${self.initial_balance:.2f}")
        self.logger.raw(f"  Ending Balance:     ${self.balance:.2f}")
        self.logger.raw(f"  Total Fees Paid:    ${self.total_fees_paid:.4f}")
        self.logger.raw(f"  Net PnL:            ${pnl:+.2f} ({pnl_pct:+.1f}%)")
        self.logger.raw("")
        self.logger.raw(f"  Total Buys:         {len(buys)}")
        self.logger.raw(f"  Total Sells:        {len(sells)}")
        if buys:
            total_cost = sum(t.get("total_cost", t.get("cost", 0)) for t in buys)
            buy_fees = sum(t.get("fee", 0) for t in buys)
            self.logger.raw(f"  Total Buy Cost:     ${total_cost:.2f} (incl ${buy_fees:.4f} fees)")
        if sells:
            total_proceeds = sum(t.get("net_proceeds", t.get("proceeds", 0)) for t in sells)
            sell_fees = sum(t.get("fee", 0) for t in sells)
            self.logger.raw(f"  Total Sell Proceeds:${total_proceeds:.2f} (after ${sell_fees:.4f} fees)")
        self.logger.raw("")
        self.logger.raw("=" * 80)

# ============================================================================
# EXCEL TRADE LOG
# ============================================================================

@dataclass
class TradeRecord:
    """Complete trade record for Excel export"""
    trade_id: int
    window_slug: str
    side: str  # "UP" or "DOWN"
    
    # Entry details
    entry_time: str
    entry_timestamp: float
    entry_price: float
    entry_bid: float
    entry_ask: float
    entry_spread: float
    entry_fee: float
    position_size: float
    position_cost: float
    
    # Exit details
    exit_time: str = ""
    exit_timestamp: float = 0.0
    exit_type: str = ""  # "TP", "SL", "HELD"
    exit_price: float = 0.0
    exit_bid: float = 0.0
    exit_ask: float = 0.0
    exit_spread: float = 0.0
    exit_fee: float = 0.0
    exit_proceeds: float = 0.0
    
    # PnL
    hold_time_secs: float = 0.0
    gross_pnl_cents: float = 0.0
    total_fees_cents: float = 0.0
    net_pnl_cents: float = 0.0
    net_pnl_dollars: float = 0.0
    
    # Balance tracking
    balance_before_trade: float = 0.0
    balance_after_trade: float = 0.0
    
    # Win/Loss
    is_win: bool = False
    
    def to_dict(self) -> dict:
        """Convert to dictionary for DataFrame"""
        return {
            "Trade #": self.trade_id,
            "Window": self.window_slug,
            "Side": self.side,
            "Entry Time": self.entry_time,
            "Entry Price": self.entry_price,
            "Entry Bid": self.entry_bid,
            "Entry Ask": self.entry_ask,
            "Entry Spread (c)": self.entry_spread,
            "Entry Fee ($)": self.entry_fee,
            "Position Size": self.position_size,
            "Cost ($)": self.position_cost,
            "Exit Time": self.exit_time,
            "Exit Type": self.exit_type,
            "Exit Price": self.exit_price,
            "Exit Bid": self.exit_bid,
            "Exit Ask": self.exit_ask,
            "Exit Spread (c)": self.exit_spread,
            "Exit Fee ($)": self.exit_fee,
            "Proceeds ($)": self.exit_proceeds,
            "Hold Time (s)": round(self.hold_time_secs, 1),
            "Gross PnL (c)": round(self.gross_pnl_cents, 2),
            "Total Fees (c)": round(self.total_fees_cents, 2),
            "Net PnL (c)": round(self.net_pnl_cents, 2),
            "Net PnL ($)": round(self.net_pnl_dollars, 4),
            "Balance Before": round(self.balance_before_trade, 2),
            "Balance After": round(self.balance_after_trade, 2),
            "Win": "YES" if self.is_win else "NO"
        }


class ExcelTradeLog:
    """Logs trades continuously to Excel file"""
    
    def __init__(self, filename: str, logger: DualLogger):
        self.filename = filename
        self.logger = logger
        self.trades: List[TradeRecord] = []
        self.trade_counter = 0
        self.wins = 0
        self.losses = 0
    
    def create_entry(
        self,
        window_slug: str,
        side: str,
        entry_price: float,
        entry_bid: float,
        entry_ask: float,
        entry_fee: float,
        position_size: float,
        position_cost: float,
        balance_before: float
    ) -> TradeRecord:
        """Create a new trade record at entry"""
        self.trade_counter += 1
        now = datetime.now()
        
        record = TradeRecord(
            trade_id=self.trade_counter,
            window_slug=window_slug,
            side=side,
            entry_time=now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            entry_timestamp=time.time(),
            entry_price=entry_price,
            entry_bid=entry_bid,
            entry_ask=entry_ask,
            entry_spread=(entry_ask - entry_bid) * 100,
            entry_fee=entry_fee,
            position_size=position_size,
            position_cost=position_cost,
            balance_before_trade=balance_before
        )
        
        self.trades.append(record)
        return record
    
    def complete_trade(
        self,
        record: TradeRecord,
        exit_type: str,
        exit_price: float,
        exit_bid: float,
        exit_ask: float,
        exit_fee: float,
        exit_proceeds: float,
        gross_pnl_cents: float,
        net_pnl_cents: float,
        net_pnl_dollars: float,
        balance_after: float
    ):
        """Complete the trade record with exit details"""
        now = datetime.now()
        
        record.exit_time = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        record.exit_timestamp = time.time()
        record.exit_type = exit_type
        record.exit_price = exit_price
        record.exit_bid = exit_bid
        record.exit_ask = exit_ask
        record.exit_spread = (exit_ask - exit_bid) * 100
        record.exit_fee = exit_fee
        record.exit_proceeds = exit_proceeds
        record.hold_time_secs = record.exit_timestamp - record.entry_timestamp
        record.gross_pnl_cents = gross_pnl_cents
        record.total_fees_cents = (record.entry_fee + exit_fee) / record.position_size * 100  # per contract
        record.net_pnl_cents = net_pnl_cents
        record.net_pnl_dollars = net_pnl_dollars
        record.balance_after_trade = balance_after
        record.is_win = net_pnl_dollars > 0
        
        if record.is_win:
            self.wins += 1
        else:
            self.losses += 1
        
        # Save to Excel after each completed trade
        self._save_to_excel()
    
    def _save_to_excel(self):
        """Save trades to Excel file"""
        if not EXCEL_AVAILABLE:
            return
        
        try:
            df = pd.DataFrame([t.to_dict() for t in self.trades])
            
            # Add summary statistics at the bottom
            total_trades = len(self.trades)
            completed_trades = len([t for t in self.trades if t.exit_time])
            total_pnl = sum(t.net_pnl_dollars for t in self.trades if t.exit_time)
            win_rate = (self.wins / completed_trades * 100) if completed_trades > 0 else 0
            avg_win = 0
            avg_loss = 0
            
            winning_trades = [t for t in self.trades if t.exit_time and t.is_win]
            losing_trades = [t for t in self.trades if t.exit_time and not t.is_win]
            
            if winning_trades:
                avg_win = sum(t.net_pnl_dollars for t in winning_trades) / len(winning_trades)
            if losing_trades:
                avg_loss = sum(t.net_pnl_dollars for t in losing_trades) / len(losing_trades)
            
            # Save main trades sheet
            with pd.ExcelWriter(self.filename, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name='Trades', index=False)
                
                # Summary sheet
                summary_data = {
                    'Metric': [
                        'Total Trades',
                        'Completed Trades',
                        'Wins',
                        'Losses',
                        'Win Rate (%)',
                        'Total PnL ($)',
                        'Average Win ($)',
                        'Average Loss ($)',
                        'Initial Balance ($)',
                        'Final Balance ($)',
                        'Total Return (%)'
                    ],
                    'Value': [
                        total_trades,
                        completed_trades,
                        self.wins,
                        self.losses,
                        f"{win_rate:.1f}%",
                        f"${total_pnl:.4f}",
                        f"${avg_win:.4f}",
                        f"${avg_loss:.4f}",
                        f"${INITIAL_BALANCE:.2f}",
                        f"${self.trades[-1].balance_after_trade:.2f}" if self.trades else f"${INITIAL_BALANCE:.2f}",
                        f"{((self.trades[-1].balance_after_trade / INITIAL_BALANCE - 1) * 100):.2f}%" if self.trades else "0.00%"
                    ]
                }
                summary_df = pd.DataFrame(summary_data)
                summary_df.to_excel(writer, sheet_name='Summary', index=False)
            
            self.logger.log(f"[EXCEL] Saved {len(self.trades)} trades to {self.filename}")
            
        except Exception as e:
            self.logger.log(f"[EXCEL] Error saving: {e}")
    
    def get_win_rate(self) -> float:
        """Get current win rate percentage"""
        completed = self.wins + self.losses
        return (self.wins / completed * 100) if completed > 0 else 0.0
    
    def print_summary(self):
        """Print trade summary"""
        completed = len([t for t in self.trades if t.exit_time])
        total_pnl = sum(t.net_pnl_dollars for t in self.trades if t.exit_time)
        win_rate = self.get_win_rate()
        
        self.logger.raw("")
        self.logger.raw("=" * 80)
        self.logger.raw("  📊 EXCEL TRADE LOG SUMMARY")
        self.logger.raw("=" * 80)
        self.logger.raw(f"  Total Trades:       {len(self.trades)}")
        self.logger.raw(f"  Completed:          {completed}")
        self.logger.raw(f"  Wins:               {self.wins}")
        self.logger.raw(f"  Losses:             {self.losses}")
        self.logger.raw(f"  Win Rate:           {win_rate:.1f}%")
        self.logger.raw(f"  Total Net PnL:      ${total_pnl:+.4f}")
        if self.trades and self.trades[-1].balance_after_trade > 0:
            final_bal = self.trades[-1].balance_after_trade
            total_return = (final_bal / INITIAL_BALANCE - 1) * 100
            self.logger.raw(f"  Final Balance:      ${final_bal:.2f}")
            self.logger.raw(f"  Total Return:       {total_return:+.2f}%")
        self.logger.raw(f"  Excel File:         {self.filename}")
        self.logger.raw("=" * 80)


# ============================================================================
# GOLDEN DATA COLLECTOR
# ============================================================================

class FillDataCollector:
    """Collects golden fill data for analysis"""
    
    def __init__(self, logger: DualLogger):
        self.fills: List[FillRecord] = []
        self.window_stats: List[WindowStats] = []
        self.current_window: Optional[WindowStats] = None
        self.logger = logger
    
    def set_window(self, slug: str, start: float, end: float):
        if self.current_window:
            self.window_stats.append(self.current_window)
        self.current_window = WindowStats(slug=slug, start_time=start, end_time=end)
    
    def record_fill(
        self,
        order_type: str,
        side: str,
        action: str,
        price: float,
        size: float,
        book: BookState
    ) -> FillRecord:
        now = time.time()
        secs_into = int(now - self.current_window.start_time) if self.current_window else 0
        secs_remaining = int(self.current_window.end_time - now) if self.current_window else 0
        
        record = FillRecord(
            order_type=order_type,
            side=side,
            action=action,
            price=price,
            size=size,
            timestamp=now,
            time_str=datetime.now().strftime("%H:%M:%S.%f")[:-3],
            bid_at_fill=book.best_bid,
            ask_at_fill=book.best_ask,
            spread_at_fill=(book.best_ask - book.best_bid) * 100,
            secs_into_window=secs_into,
            secs_remaining=secs_remaining,
            window_slug=self.current_window.slug if self.current_window else ""
        )
        
        self.fills.append(record)
        
        # Update window stats
        if self.current_window:
            if order_type == "ENTRY":
                self.current_window.entries += 1
            elif order_type == "TP":
                self.current_window.tp_exits += 1
            elif order_type == "SL":
                self.current_window.sl_exits += 1
        
        return record
    
    def add_pnl(self, pnl: float):
        if self.current_window:
            self.current_window.total_pnl += pnl
    
    def finalize(self):
        if self.current_window:
            self.window_stats.append(self.current_window)
            self.current_window = None
    
    def print_summary(self):
        self.finalize()
        
        summary = "\n" + "="*80
        summary += "\n  GOLDEN FILL DATA - MULTI-WINDOW SUMMARY"
        summary += "\n" + "="*80
        
        if not self.fills:
            summary += "\n  No fills recorded"
            self.logger.raw(summary)
            return
        
        # Per-window summary
        summary += "\n\n  WINDOW BREAKDOWN:"
        summary += "\n  " + "-"*76
        
        total_entries = 0
        total_tp = 0
        total_sl = 0
        total_pnl = 0.0
        
        for ws in self.window_stats:
            total_entries += ws.entries
            total_tp += ws.tp_exits
            total_sl += ws.sl_exits
            total_pnl += ws.total_pnl
            
            summary += f"\n  {ws.slug}"
            summary += f"\n    Entries: {ws.entries}, TP: {ws.tp_exits}, SL: {ws.sl_exits}, PnL: {ws.total_pnl:+.1f}c"
        
        summary += "\n\n  " + "-"*76
        summary += f"\n  TOTALS: Entries={total_entries}, TP={total_tp}, SL={total_sl}, PnL={total_pnl:+.1f}c"
        
        # All fills detail
        summary += "\n\n  FILL DETAILS:"
        summary += "\n  " + "-"*76
        
        for i, f in enumerate(self.fills):
            summary += f"\n\n  [{i+1}] {f.order_type} - {f.window_slug}"
            summary += f"\n      Time:     {f.time_str}"
            summary += f"\n      Side:     {f.side}"
            summary += f"\n      Action:   {f.action}"
            summary += f"\n      Price:    {f.price:.4f}"
            summary += f"\n      Size:     {f.size:.2f}"
            summary += f"\n      Bid:      {f.bid_at_fill:.4f}"
            summary += f"\n      Ask:      {f.ask_at_fill:.4f}"
            summary += f"\n      Spread:   {f.spread_at_fill:.1f}c"
            summary += f"\n      Window:   {f.secs_into_window}s in, {f.secs_remaining}s left"
        
        # Trade pairs
        entries = [f for f in self.fills if f.order_type == "ENTRY"]
        exits = [f for f in self.fills if f.order_type in ["TP", "SL"]]
        
        if entries and exits:
            summary += "\n\n  TRADE PAIRS:"
            summary += "\n  " + "-"*76
            
            for i, (entry, exit_fill) in enumerate(zip(entries, exits)):
                hold_time = exit_fill.timestamp - entry.timestamp
                pnl = (exit_fill.price - entry.price) * 100 * entry.size
                
                summary += f"\n\n  Trade {i+1}:"
                summary += f"\n      Entry:    {entry.price:.4f} @ {entry.time_str}"
                summary += f"\n      Exit:     {exit_fill.price:.4f} @ {exit_fill.time_str} ({exit_fill.order_type})"
                summary += f"\n      Hold:     {hold_time:.1f}s"
                summary += f"\n      PnL:      {pnl:+.1f}c"
        
        summary += "\n\n" + "="*80
        
        self.logger.raw(summary)

# ============================================================================
# CLOB CLIENT
# ============================================================================

class TradingClient:
    """Real CLOB client for order execution"""
    
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.client: Optional[ClobClient] = None
        
        if not dry_run and API_CONFIG:
            self._init_client()
    
    def _init_client(self):
        """Initialize real CLOB client"""
        try:
            self.client = ClobClient(
                host=CLOB_HOST,
                key=API_CONFIG.get("private_key", ""),
                chain_id=POLYGON,
                signature_type=API_CONFIG.get("signature_type", 1),
                funder=API_CONFIG.get("proxy_address", ""),
            )
            
            creds = ApiCreds(
                api_key=API_CONFIG.get("api_key", ""),
                api_secret=API_CONFIG.get("api_secret", ""),
                api_passphrase=API_CONFIG.get("api_passphrase", ""),
            )
            self.client.set_api_creds(creds)
            print(f"[CLIENT] Real CLOB client initialized")
        except Exception as e:
            print(f"[CLIENT] Error initializing: {e}")
            self.client = None
    
    def get_balance(self) -> float:
        if self.dry_run:
            return 26.0
        
        # Try multiple RPC endpoints
        rpc_endpoints = [
            "https://polygon-rpc.com",
            "https://rpc-mainnet.matic.quiknode.pro",
            "https://polygon.llamarpc.com",
        ]
        
        for rpc_url in rpc_endpoints:
            try:
                from web3 import Web3
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 10}))
                usdc_addr = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                usdc_abi = [{"constant": True, "inputs": [{"name": "account", "type": "address"}],
                            "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]
                usdc = w3.eth.contract(address=Web3.to_checksum_address(usdc_addr), abi=usdc_abi)
                bal = usdc.functions.balanceOf(Web3.to_checksum_address(API_CONFIG.get("proxy_address", ""))).call()
                return bal / 1e6
            except Exception as e:
                continue  # Try next RPC
        
        print(f"      [WARN] All RPC endpoints failed for balance check")
        return -1.0  # Return -1 to indicate failure (caller should handle)
    
    def buy_market(self, token_id: str, price: float, size: float, max_retries: int = 1) -> dict:
        """
        Place BUY order - OPTIMISTIC APPROACH (no settlement wait).
        
        FLOW:
        1. Place order
        2. Poll for MATCHED status (~2-3s)
        3. Return immediately after MATCHED (don't wait for MINED/settlement)
        4. Caller creates position optimistically
        5. SELL will retry if balance not ready yet
        
        Returns order_id for tracking MINED status via /data/trades.
        """
        if self.dry_run:
            return {"success": True, "fill_price": price, "fill_size": size, "dry_run": True}
        
        if not self.client:
            return {"success": False, "error": "Client not initialized"}
        
        price = round(price, 2)
        size = round(size, 2)
        
        # Get balance BEFORE for later verification
        balance_before = self.get_balance()
        order_placed = False
        order_id = ""
        
        try:
            args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
            signed_order = self.client.create_order(args)
            result = self.client.post_order(signed_order, OrderType.GTC)
            
            if result and result.get("success"):
                order_id = result.get("orderID", "")
                order_placed = True
                
                # Poll for MATCHED - wait up to 60s (don't give up early!)
                max_poll_time = 60  # 60s max - GTC orders can take time
                poll_interval = 0.1
                poll_start = time.time()
                order_matched = False
                last_log_time = 0
                
                print(f"      [BUY] Order placed, polling for MATCHED (max 60s)...")
                while time.time() - poll_start < max_poll_time:
                    try:
                        order_status = self.client.get_order(order_id)
                        status = order_status.get('status', '')
                        elapsed = time.time() - poll_start
                        
                        if status == "MATCHED":
                            print(f"      [BUY] MATCHED in {elapsed:.1f}s!")
                            order_matched = True
                            break
                        elif status == "LIVE":
                            if elapsed - last_log_time >= 5.0:  # Log every 5s to reduce spam
                                print(f"      [BUY] LIVE... ({elapsed:.0f}s)")
                                last_log_time = elapsed
                            time.sleep(poll_interval)
                        else:
                            print(f"      [BUY] Status: {status} - stopping")
                            break
                    except Exception as e:
                        time.sleep(poll_interval)
                
                if not order_matched:
                    # CANCEL the order to prevent orphan orders!
                    print(f"      [BUY] Not matched after {max_poll_time}s - CANCELLING order...")
                    try:
                        self.client.cancel(order_id)
                        print(f"      [BUY] Order cancelled")
                    except Exception as e:
                        print(f"      [BUY] Cancel failed: {e}")
                    
                    # Check balance to see if it filled anyway during cancel
                    balance_after = self.get_balance()
                    spent = balance_before - balance_after if balance_after > 0 else 0
                    if spent > 0.5:
                        print(f"      [BUY] Balance dropped ${spent:.2f} - order may have filled")
                        order_matched = True
                    else:
                        print(f"      [BUY] Order truly failed - no position created")
                        return {"success": False, "error": "Order not matched and cancelled", "order_placed": False, "order_id": ""}
                
                # OPTIMISTIC: Return immediately after MATCHED (no settlement wait!)
                # Use requested size - actual fill size will be verified when selling
                return {
                    "success": True, 
                    "fill_price": price, 
                    "fill_size": size,  # Use requested, will verify on sell
                    "order_id": order_id,
                    "order_placed": True,
                    "balance_before": balance_before  # For later verification
                }
            else:
                error = result.get("errorMsg", "Unknown") if result else "No response"
                return {"success": False, "error": error, "order_placed": False}
                
        except Exception as e:
            return {
                "success": False, 
                "error": str(e), 
                "order_placed": order_placed,
                "order_id": order_id
            }
    
    def sell_market(self, token_id: str, price: float, size: float, max_retries: int = 10, buy_order_id: str = "") -> dict:
        """
        Place SELL order with MINED-aware retry logic.
        
        OPTIMISTIC APPROACH:
        - If sell fails with "balance" error, wait for BUY to be MINED
        - Poll /data/trades and CLOB balance until shares available
        - Use CLOB balance (/ 1e6) as actual sellable amount
        """
        if self.dry_run:
            return {"success": True, "fill_price": price, "fill_size": size, "dry_run": True}
        
        if not self.client:
            return {"success": False, "error": "Client not initialized"}
        
        from py_clob_client.clob_types import BalanceAllowanceParams, TradeParams
        
        price = round(price, 2)
        # CRITICAL: Floor size to 1 decimal place to avoid "not enough balance" errors
        # position.size might be 6.8 but actual fill was 6.7863, so floor to 6.7
        import math
        size = math.floor(size * 10) / 10  # Floor to 1 decimal place
        original_size = size
        proxy_address = API_CONFIG.get("proxy_address", "")
        
        for attempt in range(max_retries):
            try:
                # Log first attempt size (floored)
                if attempt == 0:
                    print(f"      [SELL] Attempt #1: selling {size:.1f} shares (floored from input)")
                
                # On retries, check CLOB balance for actual sellable amount
                if attempt > 0:
                    try:
                        # Refresh CLOB cache
                        self.client.update_balance_allowance(
                            BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id)
                        )
                        ba = self.client.get_balance_allowance(
                            BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id)
                        )
                        clob_balance = float(ba.get("balance", "0") or "0") / 1e6
                        
                        if clob_balance >= 5.0:
                            # Use CLOB balance as sell size - FLOOR to 1 decimal place
                            # CRITICAL: Must floor to avoid "not enough balance" errors
                            size = math.floor(clob_balance * 10) / 10  # 6.7863 → 6.7
                            print(f"      [SELL] Retry #{attempt+1}: CLOB balance = {clob_balance:.4f}, selling {size:.1f} (floored)")
                        else:
                            print(f"      [SELL] Retry #{attempt+1}: CLOB balance = {clob_balance:.4f} (waiting for MINED...)")
                            
                            # Check if BUY is MINED via /data/trades
                            if buy_order_id:
                                try:
                                    trades = self.client.get_trades(TradeParams(maker_address=proxy_address))
                                    our_trades = [t for t in trades if t.get("taker_order_id") == buy_order_id]
                                    if our_trades:
                                        status = our_trades[0].get("status", "")
                                        print(f"      [SELL] BUY order status: {status}")
                                        if status not in ["MINED", "CONFIRMED"]:
                                            time.sleep(2.0)  # Wait more for MINED
                                            continue
                                except:
                                    pass
                            
                            time.sleep(2.0)  # Wait for MINED
                            continue
                    except Exception as e:
                        print(f"      [SELL] Balance check error: {e}")
                
                # Size already floored - just ensure it's valid
                if size < 5.0:
                    print(f"      [SELL] Size too small: {size} (need min 5)")
                    return {"success": False, "error": f"Size {size} below minimum", "attempts": attempt + 1}
                
                # CRITICAL: Use aggressive price for IMMEDIATE fill!
                # Sell at price - 0.10 (10c lower) to guarantee instant execution
                # This is a "market sell" - we'll get the best available bid
                aggressive_price = max(0.01, price - 0.10)  # At least 1c, but 10c below bid
                print(f"      [SELL] Using aggressive price: {aggressive_price:.2f} (bid was {price:.2f})")
                
                args = OrderArgs(token_id=token_id, price=aggressive_price, size=size, side=SELL)
                signed_order = self.client.create_order(args)
                result = self.client.post_order(signed_order, OrderType.GTC)
                
                if result and result.get("success"):
                    order_id = result.get("orderID", "")
                    
                    # ===== POLL FOR MATCHED STATUS =====
                    max_poll_time = 10  # 10 seconds (aggressive price should fill fast)
                    poll_interval = 0.1
                    poll_start = time.time()
                    order_matched = False
                    last_log_time = 0
                    
                    print(f"      [SELL] Order placed, polling for MATCHED...")
                    while time.time() - poll_start < max_poll_time:
                        try:
                            order_status = self.client.get_order(order_id)
                            status = order_status.get('status', '')
                            elapsed = time.time() - poll_start
                            
                            if status == "MATCHED":
                                print(f"      [SELL] MATCHED in {elapsed:.1f}s!")
                                order_matched = True
                                break
                            elif status == "LIVE":
                                if elapsed - last_log_time >= 5.0:
                                    print(f"      [SELL] LIVE... ({elapsed:.0f}s)")
                                    last_log_time = elapsed
                                time.sleep(poll_interval)
                            else:
                                print(f"      [SELL] Status: {status} - stopping")
                                break
                        except Exception as e:
                            time.sleep(poll_interval)
                    
                    if not order_matched:
                        # Order not matched - try to cancel and retry
                        print(f"      [SELL] WARNING: Not MATCHED after {max_poll_time}s!")
                        try:
                            self.client.cancel(order_id)
                            print(f"      [SELL] Order cancelled, will retry...")
                        except:
                            pass
                        time.sleep(1.0)
                        continue  # Retry the sell
                    
                    # ===== ORDER MATCHED - Get actual sold size =====
                    candidates = []
                    making_amount = result.get("makingAmount")
                    if making_amount:
                        try:
                            sold = float(making_amount)
                            if sold > 0:
                                candidates.append(sold)
                        except:
                            pass
                    
                    # Get fresh order status for size_matched
                    try:
                        order_status = self.client.get_order(order_id)
                        size_matched = order_status.get("size_matched")
                        if size_matched:
                            candidates.append(float(size_matched))
                    except:
                        pass
                    
                    actual_sold_size = min(candidates) if candidates else size
                    
                    return {
                        "success": True, 
                        "fill_price": price, 
                        "fill_size": round(actual_sold_size, 2),
                        "order_id": order_id,
                        "attempt": attempt + 1,
                        "matched": True
                    }
                
                # SELL failed - check if it's a balance error (need to wait for MINED)
                error = result.get("errorMsg", str(result)) if result else "No response"
                is_balance_error = "balance" in str(error).lower() or "allowance" in str(error).lower()
                
                if is_balance_error and attempt < max_retries - 1:
                    print(f"      [SELL] Balance not ready (attempt {attempt+1}), waiting for MINED...")
                    time.sleep(2.0)
                    continue
                elif attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                    
                return {"success": False, "error": error, "attempts": max_retries}
                
            except Exception as e:
                error_str = str(e)
                is_balance_error = "balance" in error_str.lower() or "allowance" in error_str.lower()
                
                if is_balance_error and attempt < max_retries - 1:
                    print(f"      [SELL] Balance error (attempt {attempt+1}): {error_str[:50]}...")
                    time.sleep(2.0)
                    continue
                elif attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                return {"success": False, "error": error_str, "attempts": max_retries}
        
        return {"success": False, "error": "Max retries exceeded", "attempts": max_retries}
    
    def cancel_all(self):
        if self.dry_run:
            return
        if self.client:
            try:
                self.client.cancel_all()
            except:
                pass

# ============================================================================
# MARKET RESOLUTION
# ============================================================================

def get_current_window_slug() -> Tuple[str, int, int]:
    """Get current 15-min window slug, start, and end times"""
    ts = int(time.time())
    start = ts - (ts % 900)
    end = start + 900
    return f"btc-updown-15m-{start}", start, end

def get_next_window_slug() -> Tuple[str, int, int]:
    """Get next 15-min window slug, start, and end times"""
    ts = int(time.time())
    current_start = ts - (ts % 900)
    next_start = current_start + 900
    next_end = next_start + 900
    return f"btc-updown-15m-{next_start}", next_start, next_end

def fetch_market_by_slug(slug: str, logger: DualLogger) -> Optional[dict]:
    """Fetch market by specific slug"""
    try:
        resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        markets = resp.json()
        
        if markets:
            m = markets[0]
            tokens = m.get("clobTokenIds", [])
            outcomes = m.get("outcomes", [])
            
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            token_map = {}
            for outcome, token_id in zip(outcomes, tokens):
                token_map[outcome.lower()] = token_id
            
            # Parse start/end from slug
            parts = slug.split("-")
            start_time = int(parts[-1])
            end_time = start_time + 900
            
            return {
                "slug": slug,
                "question": m.get("question", slug),
                "start_time": start_time,
                "end_time": end_time,
                "up_token": token_map.get("up"),
                "down_token": token_map.get("down"),
            }
        return None
    except Exception as e:
        logger.log(f"[MARKET] Error fetching {slug}: {e}")
        return None

def fetch_active_market(logger: DualLogger) -> Optional[dict]:
    slug, start_time, end_time = get_current_window_slug()
    secs_left = end_time - int(time.time())
    
    logger.log(f"[MARKET] Looking for: {slug}")
    logger.log(f"[MARKET] Window ends in: {secs_left // 60}:{secs_left % 60:02d}")
    
    market = fetch_market_by_slug(slug, logger)
    if market:
        logger.log(f"[MARKET] {market['question']}")
    return market

# ============================================================================
# WEBSOCKET MESSAGE PARSING
# ============================================================================

def update_book_from_message(data: dict, up_book: BookState, down_book: BookState):
    asset_id = data.get("asset_id", "")
    
    if asset_id == up_book.token_id:
        book = up_book
    elif asset_id == down_book.token_id:
        book = down_book
    else:
        return
    
    bids = data.get("bids", [])
    asks = data.get("asks", [])
    
    if bids:
        best_bid = max(bids, key=lambda x: float(x.get("price", 0)))
        book.best_bid = float(best_bid.get("price", 0))
    if asks:
        best_ask = min(asks, key=lambda x: float(x.get("price", 1)))
        book.best_ask = float(best_ask.get("price", 1))
    
    if book.best_bid > 0 and book.best_ask > 0:
        book.mid = (book.best_bid + book.best_ask) / 2
    book.timestamp = time.time()

def update_book_from_price_change(change: dict, up_book: BookState, down_book: BookState):
    asset_id = change.get("asset_id", "")
    
    if asset_id == up_book.token_id:
        book = up_book
    elif asset_id == down_book.token_id:
        book = down_book
    else:
        return
    
    best_bid = change.get("best_bid")
    best_ask = change.get("best_ask")
    
    if best_bid:
        book.best_bid = float(best_bid)
    if best_ask:
        book.best_ask = float(best_ask)
    
    if book.best_bid > 0 and book.best_ask > 0:
        book.mid = (book.best_bid + book.best_ask) / 2
    book.timestamp = time.time()

# ============================================================================
# MAIN TEST LOOP - MULTI-WINDOW
# ============================================================================

async def test_90c_flow_multiwindow():
    """Main test with TP/SL tracking across multiple windows"""
    
    # Create log file with timestamp
    log_filename = f"90c_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = DualLogger(log_filename)
    
    # Excel file for trade data
    excel_filename = f"90c_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    
    logger.raw("\n" + "="*80)
    logger.raw("  TEST 90c - DUAL STATE MACHINE (UP & DOWN INDEPENDENT)")
    logger.raw("="*80)
    logger.raw(f"  MODE: {'🔴 LIVE' if not DRY_RUN else '🟢 DRY RUN'}")
    logger.raw(f"  Entry:       {ENTRY_THRESHOLD*100:.0f}c")
    logger.raw(f"  Take Profit: {TP_THRESHOLD*100:.0f}c")
    logger.raw(f"  Stop Loss:   {SL_THRESHOLD*100:.0f}c")
    logger.raw(f"  Capital:     {CAPITAL_PERCENTAGE*100:.0f}% per trade (COMPOUNDING)")
    logger.raw(f"  Initial:     ${INITIAL_BALANCE:.2f}")
    logger.raw(f"  Duration:    {RUN_DURATION_SECS // 60} minutes")
    logger.raw(f"  Log file:    {log_filename}")
    logger.raw(f"  Excel file:  {excel_filename}")
    logger.raw("")
    logger.raw("  FEES: Polymarket taker fee = p*(1-p)*6.24%")
    logger.raw(f"         @{ENTRY_THRESHOLD*100:.0f}c entry: ~{calculate_fee_cents(ENTRY_THRESHOLD):.2f}c | @{TP_THRESHOLD*100:.0f}c exit: ~{calculate_fee_cents(TP_THRESHOLD):.2f}c | @{SL_THRESHOLD*100:.0f}c exit: ~{calculate_fee_cents(SL_THRESHOLD):.2f}c")
    logger.raw("")
    logger.raw(f"  Entry Window: Last {ENTRY_WINDOW_SECS // 60} minutes only")
    logger.raw("  LOGIC: Entry in last 6min | SL→Locked⛔ | TP→Locked⛔ | UP≠DOWN (independent)")
    logger.raw("="*80 + "\n")
    
    if not DRY_RUN:
        logger.raw("⚠️  LIVE MODE - Press Ctrl+C within 5s to abort...")
        await asyncio.sleep(5)
    
    # Initialize
    client = TradingClient(dry_run=DRY_RUN)
    collector = FillDataCollector(logger)
    
    # Get initial balance - paper or real
    if DRY_RUN:
        initial_balance = INITIAL_BALANCE
        logger.log(f"[BALANCE] Paper trading: ${initial_balance:.2f}")
    else:
        initial_balance = client.get_balance()
        if initial_balance <= 0:
            logger.log("[ERROR] Could not fetch real USDC balance! Check your wallet.")
            logger.log("[ERROR] Make sure proxy_address in pm_api_config.json is correct.")
            return
        logger.log(f"[BALANCE] Real USDC balance: ${initial_balance:.2f}")
    
    balance_tracker = BalanceTracker(initial_balance, logger)
    excel_log = ExcelTradeLog(excel_filename, logger)
    
    logger.log(f"[BALANCE] Starting: ${initial_balance:.2f}")
    logger.log(f"[CAPITAL] Using {CAPITAL_PERCENTAGE*100:.0f}% = ${initial_balance * CAPITAL_PERCENTAGE:.2f} per trade")
    
    run_start_time = time.time()
    run_end_time = run_start_time + RUN_DURATION_SECS
    window_count = 0
    total_trades = 0
    
    try:
        while time.time() < run_end_time:
            window_count += 1
            
            # Get market for current window
            market = fetch_active_market(logger)
            if not market:
                logger.log("[ERROR] No active market, waiting 30s...")
                await asyncio.sleep(30)
                continue
            
            up_token = market['up_token']
            down_token = market['down_token']
            market_start = market['start_time']
            market_end = market['end_time']
            
            collector.set_window(market['slug'], market_start, market_end)
            
            # Book state
            up_book = BookState(token_id=up_token, label="UP")
            down_book = BookState(token_id=down_token, label="DOWN")
            
            # ================================================================
            # INDEPENDENT STATE MACHINES FOR UP AND DOWN
            # ================================================================
            # UP (YES) side state
            up_position: Optional[Position] = None
            up_tp_hit: bool = False  # Locks out UP after TP OR failed entry
            
            # DOWN (NO) side state  
            down_position: Optional[Position] = None
            down_tp_hit: bool = False  # Locks out DOWN after TP OR failed entry
            # ================================================================
            
            logger.raw(f"\n{'='*80}")
            logger.raw(f"  WINDOW {window_count}: {market['slug']}")
            logger.raw(f"  Time remaining in run: {int((run_end_time - time.time()) // 60)}:{int((run_end_time - time.time()) % 60):02d}")
            logger.raw("="*80 + "\n")
            
            # Retry loop for WebSocket within same window
            reconnect_attempts = 0
            window_complete = False
            
            while not window_complete and reconnect_attempts < MAX_RECONNECT_ATTEMPTS:
                # Check if window still has time - FORCE EXIT at last 5 seconds
                secs_left = market_end - int(time.time())
                if secs_left < 5:
                    logger.log(f"\n[WINDOW] Ending in {secs_left}s, FORCE EXIT...")
                    
                    # Force close UP position if any (HELD) - WITH RETRY
                    if up_position:
                        # CRITICAL: Validate position size before selling
                        force_sell_size = up_position.size
                        if force_sell_size <= 0:
                            logger.log(f"[FORCE EXIT] UP ERROR: Invalid position size {force_sell_size}, clearing")
                            up_position = None
                        else:
                            # Round for clean sell (sell exactly what we have)
                            force_sell_size = round(force_sell_size, 1)
                            logger.log(f"[FORCE EXIT] UP position held @ {up_position.entry_price:.2f}, closing {force_sell_size} shares...")
                            
                            sell_success = False
                            sell_attempt = 0
                            max_force_attempts = 10
                            
                            while not sell_success and sell_attempt < max_force_attempts:
                                sell_attempt += 1
                                exit_price = up_book.best_bid if up_book.best_bid > 0.01 else 0.50
                                
                                if sell_attempt > 1:
                                    logger.log(f"[FORCE EXIT] UP Retry #{sell_attempt} at bid={exit_price:.2f}")
                                    await asyncio.sleep(0.3)
                                
                                result = client.sell_market(up_position.token_id, exit_price, force_sell_size, buy_order_id=up_position.buy_order_id)
                                
                                if result.get("success"):
                                    sell_success = True
                                    fill_price = result.get("fill_price", exit_price)
                                    
                                    # Calculate fees (use force_sell_size for accuracy)
                                    exit_fee = calculate_polymarket_fee(fill_price, force_sell_size)
                                    exit_fee_cents = calculate_fee_cents(fill_price)
                                    entry_fee_cents = calculate_fee_cents(up_position.entry_price)
                                    total_fees = up_position.entry_fee + exit_fee
                                    
                                    proceeds = balance_tracker.sell(fill_price, force_sell_size, fee=exit_fee)
                                    
                                    gross_pnl_cents = (fill_price - up_position.entry_price) * 100
                                    net_pnl_cents = gross_pnl_cents - entry_fee_cents - exit_fee_cents
                                    pnl_dollars = (fill_price - up_position.entry_price) * force_sell_size - total_fees
                                    
                                    collector.add_pnl(net_pnl_cents * force_sell_size)
                                    collector.record_fill("HELD", "UP", "SELL", fill_price, force_sell_size, up_book)
                                    
                                    if up_position.trade_record:
                                        excel_log.complete_trade(
                                            record=up_position.trade_record,
                                            exit_type="HELD",
                                            exit_price=fill_price,
                                            exit_bid=up_book.best_bid,
                                            exit_ask=up_book.best_ask,
                                            exit_fee=exit_fee,
                                            exit_proceeds=proceeds,
                                            gross_pnl_cents=gross_pnl_cents,
                                            net_pnl_cents=net_pnl_cents,
                                            net_pnl_dollars=pnl_dollars,
                                            balance_after=balance_tracker.balance
                                        )
                                    
                                    logger.log(f"[FORCE EXIT] UP Sold @ {fill_price:.4f}, Net PnL: ${pnl_dollars:+.4f}")
                                else:
                                    error = result.get("error", "Unknown error")
                                    logger.log(f"[FORCE EXIT] UP SELL FAILED (attempt {sell_attempt}): {error}")
                            
                            if not sell_success:
                                logger.log(f"[FORCE EXIT] CRITICAL: UP position could not be sold! Going to settlement!")
                            
                            up_position = None
                    
                    # Force close DOWN position if any (HELD) - WITH RETRY
                    if down_position:
                        # CRITICAL: Validate position size before selling
                        force_sell_size = down_position.size
                        if force_sell_size <= 0:
                            logger.log(f"[FORCE EXIT] DOWN ERROR: Invalid position size {force_sell_size}, clearing")
                            down_position = None
                        else:
                            # Round for clean sell (sell exactly what we have)
                            force_sell_size = round(force_sell_size, 1)
                            logger.log(f"[FORCE EXIT] DOWN position held @ {down_position.entry_price:.2f}, closing {force_sell_size} shares...")
                            
                            sell_success = False
                            sell_attempt = 0
                            max_force_attempts = 10
                            
                            while not sell_success and sell_attempt < max_force_attempts:
                                sell_attempt += 1
                                exit_price = down_book.best_bid if down_book.best_bid > 0.01 else 0.50
                                
                                if sell_attempt > 1:
                                    logger.log(f"[FORCE EXIT] DOWN Retry #{sell_attempt} at bid={exit_price:.2f}")
                                    await asyncio.sleep(0.3)
                                
                                result = client.sell_market(down_position.token_id, exit_price, force_sell_size, buy_order_id=down_position.buy_order_id)
                                
                                if result.get("success"):
                                    sell_success = True
                                    fill_price = result.get("fill_price", exit_price)
                                    
                                    # Calculate fees (use force_sell_size for accuracy)
                                    exit_fee = calculate_polymarket_fee(fill_price, force_sell_size)
                                    exit_fee_cents = calculate_fee_cents(fill_price)
                                    entry_fee_cents = calculate_fee_cents(down_position.entry_price)
                                    total_fees = down_position.entry_fee + exit_fee
                                    
                                    proceeds = balance_tracker.sell(fill_price, force_sell_size, fee=exit_fee)
                                    
                                    gross_pnl_cents = (fill_price - down_position.entry_price) * 100
                                    net_pnl_cents = gross_pnl_cents - entry_fee_cents - exit_fee_cents
                                    pnl_dollars = (fill_price - down_position.entry_price) * force_sell_size - total_fees
                                    
                                    collector.add_pnl(net_pnl_cents * force_sell_size)
                                    collector.record_fill("HELD", "DOWN", "SELL", fill_price, force_sell_size, down_book)
                                    
                                    if down_position.trade_record:
                                        excel_log.complete_trade(
                                            record=down_position.trade_record,
                                            exit_type="HELD",
                                            exit_price=fill_price,
                                            exit_bid=down_book.best_bid,
                                            exit_ask=down_book.best_ask,
                                            exit_fee=exit_fee,
                                            exit_proceeds=proceeds,
                                            gross_pnl_cents=gross_pnl_cents,
                                            net_pnl_cents=net_pnl_cents,
                                            net_pnl_dollars=pnl_dollars,
                                            balance_after=balance_tracker.balance
                                        )
                                    
                                    logger.log(f"[FORCE EXIT] DOWN Sold @ {fill_price:.4f}, Net PnL: ${pnl_dollars:+.4f}")
                                else:
                                    error = result.get("error", "Unknown error")
                                    logger.log(f"[FORCE EXIT] DOWN SELL FAILED (attempt {sell_attempt}): {error}")
                            
                            if not sell_success:
                                logger.log(f"[FORCE EXIT] CRITICAL: DOWN position could not be sold! Going to settlement!")
                            
                            down_position = None
                        
                        down_position = None
                    
                    # ============================================================
                    # END OF WINDOW: Sync balance with blockchain (LIVE ONLY)
                    # ============================================================
                    if not DRY_RUN:
                        logger.log(f"[SYNC] Waiting 6s for blockchain settlement...")
                        await asyncio.sleep(6.0)
                        
                        real_balance = client.get_balance()
                        old_balance = balance_tracker.balance
                        balance_tracker.balance = real_balance
                        
                        diff = real_balance - old_balance
                        logger.log(f"[SYNC] Paper balance: ${old_balance:.4f}")
                        logger.log(f"[SYNC] Real balance:  ${real_balance:.4f}")
                        logger.log(f"[SYNC] Difference:    ${diff:+.4f}")
                        logger.log(f"[SYNC] Using real balance for next window: ${real_balance:.4f}")
                    
                    # Print window end balance
                    balance_tracker.print_status(window_count, market['slug'])
                    
                    window_complete = True
                    break
                
                if reconnect_attempts > 0:
                    logger.log(f"[WS] Reconnecting... (attempt {reconnect_attempts + 1}/{MAX_RECONNECT_ATTEMPTS})")
                else:
                    logger.log(f"[WS] Connecting...")
                
                try:
                    async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=10) as ws:
                        logger.log("[WS] Connected!")
                        reconnect_attempts = 0  # Reset on successful connection
                        
                        await ws.send(json.dumps({"type": "MARKET", "assets_ids": [up_token, down_token]}))
                        logger.log(f"[WS] Subscribed\n")
                        
                        # Report preserved positions
                        if up_position:
                            logger.log(f"[WS] UP position preserved: @ {up_position.entry_price:.2f}")
                        if down_position:
                            logger.log(f"[WS] DOWN position preserved: @ {down_position.entry_price:.2f}")
                        
                        logger.raw("-"*80)
                        logger.raw(f"  MONITORING - Entry@{ENTRY_THRESHOLD*100:.0f}c, TP@{TP_THRESHOLD*100:.0f}c, SL@{SL_THRESHOLD*100:.0f}c")
                        logger.raw(f"  Log frequency: {LOG_INTERVAL_SECS*1000:.0f}ms ({'VERBOSE' if DRY_RUN else 'NORMAL'})")
                        logger.raw("-"*80 + "\n")
                        
                        last_log_time = 0
                        
                        while True:
                            # Check run duration
                            if time.time() >= run_end_time:
                                logger.log(f"\n[RUN] Duration limit reached ({RUN_DURATION_SECS // 60} mins)")
                                window_complete = True
                                break
                            
                            secs_left = market_end - int(time.time())
                            if secs_left < 5:
                                logger.log(f"\n[WINDOW] Ending in {secs_left}s, FORCE EXIT...")
                                
                                # Force close UP position if any (HELD) - WITH RETRY
                                if up_position:
                                    # CRITICAL: Validate position size before selling
                                    force_sell_size = up_position.size
                                    if force_sell_size <= 0:
                                        logger.log(f"[FORCE EXIT] UP ERROR: Invalid position size {force_sell_size}, clearing")
                                        up_position = None
                                    else:
                                        # Round for clean sell (sell exactly what we have)
                                        force_sell_size = round(force_sell_size, 1)
                                        logger.log(f"[FORCE EXIT] UP position held @ {up_position.entry_price:.2f}, closing {force_sell_size} shares...")
                                        
                                        sell_success = False
                                        sell_attempt = 0
                                        max_force_attempts = 10
                                        
                                        while not sell_success and sell_attempt < max_force_attempts:
                                            sell_attempt += 1
                                            exit_price = up_book.best_bid if up_book.best_bid > 0.01 else 0.50
                                            
                                            if sell_attempt > 1:
                                                logger.log(f"[FORCE EXIT] UP Retry #{sell_attempt} at bid={exit_price:.2f}")
                                                await asyncio.sleep(0.3)
                                            
                                            result = client.sell_market(up_position.token_id, exit_price, force_sell_size, buy_order_id=up_position.buy_order_id)
                                            
                                            if result.get("success"):
                                                sell_success = True
                                                fill_price = result.get("fill_price", exit_price)
                                                
                                                # Calculate fees (use force_sell_size for accuracy)
                                                exit_fee = calculate_polymarket_fee(fill_price, force_sell_size)
                                                exit_fee_cents = calculate_fee_cents(fill_price)
                                                entry_fee_cents = calculate_fee_cents(up_position.entry_price)
                                                total_fees = up_position.entry_fee + exit_fee
                                                
                                                proceeds = balance_tracker.sell(fill_price, force_sell_size, fee=exit_fee)
                                                
                                                gross_pnl_cents = (fill_price - up_position.entry_price) * 100
                                                net_pnl_cents = gross_pnl_cents - entry_fee_cents - exit_fee_cents
                                                pnl_dollars = (fill_price - up_position.entry_price) * force_sell_size - total_fees
                                                
                                                collector.add_pnl(net_pnl_cents * force_sell_size)
                                                collector.record_fill("HELD", "UP", "SELL", fill_price, force_sell_size, up_book)
                                                
                                                if up_position.trade_record:
                                                    excel_log.complete_trade(
                                                        record=up_position.trade_record,
                                                        exit_type="HELD",
                                                        exit_price=fill_price,
                                                        exit_bid=up_book.best_bid,
                                                        exit_ask=up_book.best_ask,
                                                        exit_fee=exit_fee,
                                                        exit_proceeds=proceeds,
                                                        gross_pnl_cents=gross_pnl_cents,
                                                        net_pnl_cents=net_pnl_cents,
                                                        net_pnl_dollars=pnl_dollars,
                                                        balance_after=balance_tracker.balance
                                                    )
                                                
                                                logger.log(f"[FORCE EXIT] UP Sold @ {fill_price:.4f}, Net PnL: ${pnl_dollars:+.4f}")
                                            else:
                                                error = result.get("error", "Unknown error")
                                                logger.log(f"[FORCE EXIT] UP SELL FAILED (attempt {sell_attempt}): {error}")
                                        
                                        if not sell_success:
                                            logger.log(f"[FORCE EXIT] CRITICAL: UP position could not be sold! Going to settlement!")
                                        
                                        up_position = None
                                
                                # Force close DOWN position if any (HELD) - WITH RETRY
                                if down_position:
                                    # CRITICAL: Validate position size before selling
                                    force_sell_size = down_position.size
                                    if force_sell_size <= 0:
                                        logger.log(f"[FORCE EXIT] DOWN ERROR: Invalid position size {force_sell_size}, clearing")
                                        down_position = None
                                    else:
                                        # Round for clean sell (sell exactly what we have)
                                        force_sell_size = round(force_sell_size, 1)
                                        logger.log(f"[FORCE EXIT] DOWN position held @ {down_position.entry_price:.2f}, closing {force_sell_size} shares...")
                                        
                                        sell_success = False
                                        sell_attempt = 0
                                        max_force_attempts = 10
                                        
                                        while not sell_success and sell_attempt < max_force_attempts:
                                            sell_attempt += 1
                                            exit_price = down_book.best_bid if down_book.best_bid > 0.01 else 0.50
                                            
                                            if sell_attempt > 1:
                                                logger.log(f"[FORCE EXIT] DOWN Retry #{sell_attempt} at bid={exit_price:.2f}")
                                                await asyncio.sleep(0.3)
                                            
                                            result = client.sell_market(down_position.token_id, exit_price, force_sell_size, buy_order_id=down_position.buy_order_id)
                                            
                                            if result.get("success"):
                                                sell_success = True
                                                fill_price = result.get("fill_price", exit_price)
                                                
                                                # Calculate fees (use force_sell_size for accuracy)
                                                exit_fee = calculate_polymarket_fee(fill_price, force_sell_size)
                                                exit_fee_cents = calculate_fee_cents(fill_price)
                                                entry_fee_cents = calculate_fee_cents(down_position.entry_price)
                                                total_fees = down_position.entry_fee + exit_fee
                                                
                                                proceeds = balance_tracker.sell(fill_price, force_sell_size, fee=exit_fee)
                                                
                                                gross_pnl_cents = (fill_price - down_position.entry_price) * 100
                                                net_pnl_cents = gross_pnl_cents - entry_fee_cents - exit_fee_cents
                                                pnl_dollars = (fill_price - down_position.entry_price) * force_sell_size - total_fees
                                                
                                                collector.add_pnl(net_pnl_cents * force_sell_size)
                                                collector.record_fill("HELD", "DOWN", "SELL", fill_price, force_sell_size, down_book)
                                                
                                                if down_position.trade_record:
                                                    excel_log.complete_trade(
                                                        record=down_position.trade_record,
                                                        exit_type="HELD",
                                                        exit_price=fill_price,
                                                        exit_bid=down_book.best_bid,
                                                        exit_ask=down_book.best_ask,
                                                        exit_fee=exit_fee,
                                                        exit_proceeds=proceeds,
                                                        gross_pnl_cents=gross_pnl_cents,
                                                        net_pnl_cents=net_pnl_cents,
                                                        net_pnl_dollars=pnl_dollars,
                                                        balance_after=balance_tracker.balance
                                                    )
                                                
                                                logger.log(f"[FORCE EXIT] DOWN Sold @ {fill_price:.4f}, Net PnL: ${pnl_dollars:+.4f}")
                                            else:
                                                error = result.get("error", "Unknown error")
                                                logger.log(f"[FORCE EXIT] DOWN SELL FAILED (attempt {sell_attempt}): {error}")
                                        
                                        if not sell_success:
                                            logger.log(f"[FORCE EXIT] CRITICAL: DOWN position could not be sold! Going to settlement!")
                                        
                                        down_position = None
                                
                                # ============================================================
                                # END OF WINDOW: Sync balance with blockchain (LIVE ONLY)
                                # ============================================================
                                if not DRY_RUN:
                                    logger.log(f"[SYNC] Waiting 6s for blockchain settlement...")
                                    await asyncio.sleep(6.0)
                                    
                                    real_balance = client.get_balance()
                                    old_balance = balance_tracker.balance
                                    balance_tracker.balance = real_balance
                                    
                                    diff = real_balance - old_balance
                                    logger.log(f"[SYNC] Paper balance: ${old_balance:.4f}")
                                    logger.log(f"[SYNC] Real balance:  ${real_balance:.4f}")
                                    logger.log(f"[SYNC] Difference:    ${diff:+.4f}")
                                    logger.log(f"[SYNC] Using real balance for next window: ${real_balance:.4f}")
                                
                                # Print window end balance
                                balance_tracker.print_status(window_count, market['slug'])
                                
                                window_complete = True
                                break
                            
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                                data = json.loads(msg)
                                
                                # Update books
                                if isinstance(data, list):
                                    for item in data:
                                        update_book_from_message(item, up_book, down_book)
                                elif isinstance(data, dict):
                                    event_type = data.get("event_type", data.get("type", ""))
                                    if event_type == "price_change":
                                        for change in data.get("price_changes", []):
                                            update_book_from_price_change(change, up_book, down_book)
                                    elif event_type == "book":
                                        update_book_from_message(data, up_book, down_book)
                                
                                # Log at configured interval
                                now = time.time()
                                if now - last_log_time > LOG_INTERVAL_SECS and (up_book.valid or down_book.valid):
                                    mins = secs_left // 60
                                    secs = secs_left % 60
                                    
                                    # Build position status string
                                    pos_parts = []
                                    if up_position:
                                        pnl = (up_book.mid - up_position.entry_price) * 100
                                        pos_parts.append(f"UP@{up_position.entry_price:.2f}→{up_book.mid:.2f}({pnl:+.0f}c)")
                                    elif up_tp_hit:
                                        pos_parts.append("UP:TP⛔")
                                    
                                    if down_position:
                                        pnl = (down_book.mid - down_position.entry_price) * 100
                                        pos_parts.append(f"DN@{down_position.entry_price:.2f}→{down_book.mid:.2f}({pnl:+.0f}c)")
                                    elif down_tp_hit:
                                        pos_parts.append("DN:TP⛔")
                                    
                                    pos_str = f" | {' '.join(pos_parts)}" if pos_parts else ""
                                    
                                    logger.log(f"[{mins}:{secs:02d}] UP: {up_book.best_bid:.2f}/{up_book.best_ask:.2f} | "
                                              f"DOWN: {down_book.best_bid:.2f}/{down_book.best_ask:.2f}{pos_str}")
                                    last_log_time = now
                                
                                # ============================================================
                                # TRADING LOGIC - DUAL STATE MACHINE (UP & DOWN INDEPENDENT)
                                # ============================================================
                                
                                # -------------------- UP (YES) SIDE --------------------
                                # UP ENTRY: Check conditions including spread AND timing (last 6 min only)
                                up_spread = up_book.best_ask - up_book.best_bid if up_book.valid else 1.0
                                in_entry_window = secs_left <= ENTRY_WINDOW_SECS  # Last 6 minutes only
                                
                                # Debug: Log why entry not triggered when price is at threshold
                                if up_book.valid and up_book.best_ask >= ENTRY_THRESHOLD and up_position is None and not up_tp_hit:
                                    if not in_entry_window:
                                        # Only log occasionally to avoid spam
                                        if int(time.time()) % 30 == 0:
                                            logger.log(f"[UP] SKIP - Not in entry window ({secs_left}s left, need <= {ENTRY_WINDOW_SECS}s)")
                                    elif up_spread > 0.02:
                                        # Only log occasionally to avoid spam
                                        if int(time.time()) % 10 == 0:
                                            logger.log(f"[UP] SKIP - Spread too wide: {up_spread:.2f} (need <= 0.02)")
                                
                                if up_position is None and not up_tp_hit and up_book.valid and up_book.best_ask >= ENTRY_THRESHOLD and up_spread <= 0.02 and in_entry_window:  # 2c max spread + last 6 min
                                    # NOTE: Removed cancel_all() - it cancels ALL markets!
                                    # The 60s polling + specific order cancel handles orphan orders
                                    
                                    # Get REAL blockchain balance FIRST (critical for parallel scripts!)
                                    if not DRY_RUN:
                                        real_balance_before = client.get_balance()
                                        if real_balance_before < 0:
                                            logger.log(f"[UP ENTRY] SKIP - Balance check failed (RPC error)")
                                            continue
                                    else:
                                        real_balance_before = balance_tracker.balance
                                    
                                    # Calculate capital from REAL balance (not stale balance_tracker)
                                    entry_price = up_book.best_ask
                                    capital_to_use = real_balance_before * CAPITAL_PERCENTAGE
                                    position_size = capital_to_use / entry_price
                                    position_size = max(5.0, math.floor(position_size * 10) / 10)  # Floor to avoid exceeding balance
                                    
                                    # Check if we have enough capital for minimum position
                                    min_cost = position_size * entry_price
                                    if min_cost > capital_to_use * 1.1:  # Allow 10% buffer
                                        logger.log(f"[UP ENTRY] SKIP - Insufficient capital: need ${min_cost:.2f}, have ${capital_to_use:.2f}")
                                        continue
                                    
                                    logger.raw(f"\n{'='*80}")
                                    logger.log(f"[UP ENTRY] ask={up_book.best_ask:.2f} >= {ENTRY_THRESHOLD}, spread={up_spread:.2f}c")
                                    logger.raw(f"{'='*80}")
                                    logger.log(f"[UP ENTRY] Real balance: ${real_balance_before:.4f}, using {CAPITAL_PERCENTAGE*100:.0f}% = ${capital_to_use:.2f}")
                                    
                                    balance_before = real_balance_before
                                    
                                    # Try to buy (only 1 attempt - check balance after to detect phantom fills)
                                    result = client.buy_market(up_token, entry_price, position_size)
                                    
                                    # Check if order succeeded OR if balance dropped (phantom fill detection)
                                    buy_success = result.get("success", False)
                                    detected_fill_size = None
                                    
                                    if not buy_success and not DRY_RUN:
                                        # Order "failed" - but check if balance actually dropped (order filled anyway!)
                                        real_balance_after = client.get_balance()
                                        spent = real_balance_before - real_balance_after
                                        
                                        if spent > 0.50:  # Significant balance drop = order filled
                                            detected_fill_size = spent / entry_price
                                            logger.log(f"[UP ENTRY] PHANTOM FILL DETECTED!")
                                            logger.log(f"[UP ENTRY] Balance dropped: ${real_balance_before:.4f} -> ${real_balance_after:.4f} (spent ${spent:.4f})")
                                            logger.log(f"[UP ENTRY] Calculated fill: {detected_fill_size:.2f} shares @ {entry_price:.2f}")
                                            buy_success = True  # Treat as success
                                            result = {
                                                "success": True,
                                                "fill_price": entry_price,
                                                "fill_size": round(detected_fill_size, 1),
                                                "phantom_fill": True
                                            }
                                        else:
                                            error = result.get("error", "Unknown error")
                                            logger.log(f"[UP ENTRY] BUY FAILED: {error}")
                                            logger.log(f"[UP ENTRY] Balance unchanged: ${real_balance_after:.4f} - order truly failed")
                                    
                                    if not buy_success:
                                        logger.log(f"[UP ENTRY] FAILED - LOCKING OUT UP for this window")
                                        up_tp_hit = True  # Lock out UP side - no more entries this window
                                        continue
                                    
                                    if result.get("success"):
                                        fill_price = result.get("fill_price", entry_price)
                                        fill_size = result.get("fill_size", position_size)
                                        
                                        # Calculate entry fee
                                        entry_fee = calculate_polymarket_fee(fill_price, fill_size)
                                        entry_fee_cents = calculate_fee_cents(fill_price)
                                        
                                        cost = balance_tracker.buy(fill_price, fill_size, fee=entry_fee)
                                        entry_record = collector.record_fill("ENTRY", "UP", "BUY", fill_price, fill_size, up_book)
                                        
                                        # Create Excel trade record
                                        trade_record = excel_log.create_entry(
                                            window_slug=market['slug'],
                                            side="UP",
                                            entry_price=fill_price,
                                            entry_bid=up_book.best_bid,
                                            entry_ask=up_book.best_ask,
                                            entry_fee=entry_fee,
                                            position_size=fill_size,
                                            position_cost=cost,
                                            balance_before=balance_before
                                        )
                                        
                                        # Get real balance before entry for live mode PnL calculation
                                        if not DRY_RUN:
                                            balance_at_entry = client.get_balance()
                                        else:
                                            balance_at_entry = balance_before
                                        
                                        up_position = Position(
                                            token_id=up_token,
                                            side="UP",
                                            size=fill_size,
                                            entry_price=fill_price,
                                            entry_time=time.time(),
                                            entry_record=entry_record,
                                            entry_fee=entry_fee,
                                            trade_record=trade_record,
                                            balance_at_entry=balance_at_entry,
                                            buy_order_id=result.get("order_id", "")
                                        )
                                        
                                        total_trades += 1
                                        logger.log(f"[UP FILL] BUY {fill_size:.1f} @ {fill_price:.4f}, Cost: ${cost:.2f}")
                                        logger.log(f"[UP FILL] Entry fee: ${entry_fee:.4f} ({entry_fee_cents:.2f}c/contract)")
                                        logger.log(f"[UP FILL] Capital used: {CAPITAL_PERCENTAGE*100:.0f}% of ${balance_before:.2f} = ${capital_to_use:.2f}")
                                        
                                        logger.log(f"[UP] Waiting for TP@{TP_THRESHOLD} or SL@{SL_THRESHOLD}...")
                                        logger.raw(f"{'='*80}\n")
                                
                                # UP EXIT: Check TP or SL
                                if up_position is not None:
                                    up_mid = up_book.mid
                                    up_exit_type = None
                                    up_exit_price = 0.0
                                    
                                    if up_mid >= TP_THRESHOLD:
                                        up_exit_type = "TP"
                                        up_exit_price = up_book.best_bid
                                        logger.raw(f"\n{'='*80}")
                                        logger.log(f"[UP TP HIT] mid={up_mid:.2f} >= {TP_THRESHOLD}")
                                    elif up_mid <= SL_THRESHOLD:
                                        up_exit_type = "SL"
                                        up_exit_price = up_book.best_bid
                                        logger.raw(f"\n{'='*80}")
                                        logger.log(f"[UP SL HIT] mid={up_mid:.2f} <= {SL_THRESHOLD}")
                                    
                                    if up_exit_type:
                                        # CRITICAL: Validate position size before selling
                                        sell_size = up_position.size
                                        if sell_size <= 0:
                                            logger.log(f"[UP {up_exit_type}] ERROR: Invalid position size {sell_size}, resetting position")
                                            up_position = None
                                            continue
                                        
                                        # Round for clean sell (sell exactly what we have)
                                        sell_size = round(sell_size, 1)
                                        logger.log(f"[UP {up_exit_type}] Selling {sell_size} shares @ {up_exit_price:.2f}")
                                        
                                        # Keep retrying until sell succeeds
                                        sell_success = False
                                        sell_attempt = 0
                                        max_sell_attempts = 10  # Try up to 10 times
                                        
                                        while not sell_success and sell_attempt < max_sell_attempts:
                                            sell_attempt += 1
                                            
                                            # Check if window is ending - abandon if < 5 seconds left
                                            secs_left_now = market_end - int(time.time())
                                            if secs_left_now < 5:
                                                logger.log(f"[UP {up_exit_type}] TIMEOUT - Window ending in {secs_left_now}s, abandoning sell")
                                                break
                                            
                                            # Refresh price on retry
                                            if sell_attempt > 1:
                                                up_exit_price = up_book.best_bid
                                                logger.log(f"[UP {up_exit_type}] Retry #{sell_attempt} at bid={up_exit_price:.2f}")
                                                await asyncio.sleep(0.5)  # Brief pause before retry
                                            
                                            result = client.sell_market(up_position.token_id, up_exit_price, sell_size, buy_order_id=up_position.buy_order_id)
                                            
                                            if result.get("success"):
                                                sell_success = True
                                            else:
                                                error = result.get("error", "Unknown error")
                                                logger.log(f"[UP {up_exit_type}] SELL FAILED (attempt {sell_attempt}): {error}")
                                        
                                        if not sell_success:
                                            logger.log(f"[UP {up_exit_type}] ABANDONED - Could not sell, position lost to settlement!")
                                            up_position = None
                                            up_tp_hit = True  # Lock out to prevent new entries
                                            continue  # Move on to next window
                                        
                                        if result.get("success"):
                                            fill_price = result.get("fill_price", up_exit_price)
                                            logger.log(f"[UP {up_exit_type}] SELL {up_position.size:.1f} @ {fill_price:.4f}")
                                            
                                            # STEP 1: Wait 6 seconds for blockchain settlement (BOTH TP and SL)
                                            if not DRY_RUN:
                                                logger.log(f"[UP {up_exit_type}] Waiting 6s for blockchain settlement...")
                                                await asyncio.sleep(6.0)
                                                
                                                # STEP 2: Fetch REAL balance from chain
                                                balance_before_sell = up_position.balance_at_entry if hasattr(up_position, 'balance_at_entry') else balance_tracker.balance
                                                real_balance = client.get_balance()
                                                
                                                # STEP 3: Calculate PnL from REAL balance change
                                                pnl_dollars = real_balance - balance_before_sell
                                                
                                                # STEP 4: Update balance tracker with REAL balance
                                                balance_tracker.balance = real_balance
                                                
                                                logger.log(f"[UP {up_exit_type}] Balance before: ${balance_before_sell:.4f}")
                                                logger.log(f"[UP {up_exit_type}] Balance after:  ${real_balance:.4f}")
                                                logger.log(f"[UP {up_exit_type}] Real PnL: ${pnl_dollars:+.4f}")
                                            else:
                                                # Paper trading - use paper calculations
                                                exit_fee = calculate_polymarket_fee(fill_price, up_position.size)
                                                exit_fee_cents = calculate_fee_cents(fill_price)
                                                entry_fee_cents = calculate_fee_cents(up_position.entry_price)
                                                total_fees = up_position.entry_fee + exit_fee
                                                
                                                proceeds = balance_tracker.sell(fill_price, up_position.size, fee=exit_fee)
                                                pnl_dollars = (fill_price - up_position.entry_price) * up_position.size - total_fees
                                                
                                                gross_pnl_cents = (fill_price - up_position.entry_price) * 100
                                                net_pnl_cents = gross_pnl_cents - entry_fee_cents - exit_fee_cents
                                                logger.log(f"[UP {up_exit_type}] Gross: {gross_pnl_cents:+.1f}c, Fees: {entry_fee_cents + exit_fee_cents:.2f}c, Net: {net_pnl_cents:+.2f}c/contract")
                                            
                                            hold_time = time.time() - up_position.entry_time
                                            collector.record_fill(up_exit_type, "UP", "SELL", fill_price, up_position.size, up_book)
                                            collector.add_pnl(pnl_dollars * 100)  # Convert to cents for collector
                                            
                                            # STEP 5: Log to Excel
                                            if up_position.trade_record:
                                                exit_fee = calculate_polymarket_fee(fill_price, up_position.size) if DRY_RUN else 0
                                                gross_pnl_cents = (fill_price - up_position.entry_price) * 100
                                                net_pnl_cents = pnl_dollars * 100 / up_position.size if up_position.size > 0 else 0
                                                excel_log.complete_trade(
                                                    record=up_position.trade_record,
                                                    exit_type=up_exit_type,
                                                    exit_price=fill_price,
                                                    exit_bid=up_book.best_bid,
                                                    exit_ask=up_book.best_ask,
                                                    exit_fee=exit_fee,
                                                    exit_proceeds=fill_price * up_position.size,
                                                    gross_pnl_cents=gross_pnl_cents,
                                                    net_pnl_cents=net_pnl_cents,
                                                    net_pnl_dollars=pnl_dollars,
                                                    balance_after=balance_tracker.balance
                                                )
                                            
                                            logger.log(f"[UP {up_exit_type}] Total PnL: ${pnl_dollars:+.4f}, Hold: {hold_time:.1f}s")
                                            logger.log(f"[UP {up_exit_type}] Balance: ${balance_tracker.balance:.2f}, Win Rate: {excel_log.get_win_rate():.1f}%")
                                            
                                            # STEP 6: Set lock out flag (BOTH TP and SL)
                                            up_tp_hit = True  # Lock out after BOTH TP and SL
                                            logger.log(f"[UP] LOCKED OUT for rest of window ({up_exit_type} hit)")
                                            if up_exit_type == "TP":
                                                logger.log(f"[UP TP] DOWN can now use {CAPITAL_PERCENTAGE*100:.0f}% of ${balance_tracker.balance:.2f} = ${balance_tracker.balance * CAPITAL_PERCENTAGE:.2f}")
                                            
                                            logger.raw(f"{'='*80}\n")
                                            up_position = None
                                
                                # -------------------- DOWN (NO) SIDE --------------------
                                # DOWN ENTRY: Check conditions including spread AND timing (last 6 min only)
                                down_spread = down_book.best_ask - down_book.best_bid if down_book.valid else 1.0
                                # in_entry_window already calculated above for UP side
                                
                                # Debug: Log why entry not triggered when price is at threshold
                                if down_book.valid and down_book.best_ask >= ENTRY_THRESHOLD and down_position is None and not down_tp_hit:
                                    if not in_entry_window:
                                        # Only log occasionally to avoid spam
                                        if int(time.time()) % 30 == 0:
                                            logger.log(f"[DOWN] SKIP - Not in entry window ({secs_left}s left, need <= {ENTRY_WINDOW_SECS}s)")
                                    elif down_spread > 0.02:
                                        # Only log occasionally to avoid spam
                                        if int(time.time()) % 10 == 0:
                                            logger.log(f"[DOWN] SKIP - Spread too wide: {down_spread:.2f} (need <= 0.02)")
                                
                                if down_position is None and not down_tp_hit and down_book.valid and down_book.best_ask >= ENTRY_THRESHOLD and down_spread <= 0.02 and in_entry_window:  # 2c max spread + last 6 min
                                    # NOTE: Removed cancel_all() - it cancels ALL markets!
                                    # The 60s polling + specific order cancel handles orphan orders
                                    
                                    # Get REAL blockchain balance FIRST (critical for parallel scripts!)
                                    if not DRY_RUN:
                                        real_balance_before = client.get_balance()
                                        if real_balance_before < 0:
                                            logger.log(f"[DOWN ENTRY] SKIP - Balance check failed (RPC error)")
                                            continue
                                    else:
                                        real_balance_before = balance_tracker.balance
                                    
                                    # Calculate capital from REAL balance (not stale balance_tracker)
                                    entry_price = down_book.best_ask
                                    capital_to_use = real_balance_before * CAPITAL_PERCENTAGE
                                    position_size = capital_to_use / entry_price
                                    position_size = max(5.0, math.floor(position_size * 10) / 10)  # Floor to avoid exceeding balance
                                    
                                    # Check if we have enough capital for minimum position
                                    min_cost = position_size * entry_price
                                    if min_cost > capital_to_use * 1.1:  # Allow 10% buffer
                                        logger.log(f"[DOWN ENTRY] SKIP - Insufficient capital: need ${min_cost:.2f}, have ${capital_to_use:.2f}")
                                        continue
                                    
                                    logger.raw(f"\n{'='*80}")
                                    logger.log(f"[DOWN ENTRY] ask={down_book.best_ask:.2f} >= {ENTRY_THRESHOLD}, spread={down_spread:.2f}c")
                                    logger.raw(f"{'='*80}")
                                    logger.log(f"[DOWN ENTRY] Real balance: ${real_balance_before:.4f}, using {CAPITAL_PERCENTAGE*100:.0f}% = ${capital_to_use:.2f}")
                                    
                                    balance_before = real_balance_before
                                    
                                    # Try to buy (only 1 attempt - check balance after to detect phantom fills)
                                    result = client.buy_market(down_token, entry_price, position_size)
                                    
                                    # Check if order succeeded OR if balance dropped (phantom fill detection)
                                    buy_success = result.get("success", False)
                                    detected_fill_size = None
                                    
                                    if not buy_success and not DRY_RUN:
                                        # Order "failed" - but check if balance actually dropped (order filled anyway!)
                                        real_balance_after = client.get_balance()
                                        spent = real_balance_before - real_balance_after
                                        
                                        if spent > 0.50:  # Significant balance drop = order filled
                                            detected_fill_size = spent / entry_price
                                            logger.log(f"[DOWN ENTRY] PHANTOM FILL DETECTED!")
                                            logger.log(f"[DOWN ENTRY] Balance dropped: ${real_balance_before:.4f} -> ${real_balance_after:.4f} (spent ${spent:.4f})")
                                            logger.log(f"[DOWN ENTRY] Calculated fill: {detected_fill_size:.2f} shares @ {entry_price:.2f}")
                                            buy_success = True  # Treat as success
                                            result = {
                                                "success": True,
                                                "fill_price": entry_price,
                                                "fill_size": round(detected_fill_size, 1),
                                                "phantom_fill": True
                                            }
                                        else:
                                            error = result.get("error", "Unknown error")
                                            logger.log(f"[DOWN ENTRY] BUY FAILED: {error}")
                                            logger.log(f"[DOWN ENTRY] Balance unchanged: ${real_balance_after:.4f} - order truly failed")
                                    
                                    if not buy_success:
                                        logger.log(f"[DOWN ENTRY] FAILED - LOCKING OUT DOWN for this window")
                                        down_tp_hit = True  # Lock out DOWN side - no more entries this window
                                        continue
                                    
                                    if result.get("success"):
                                        fill_price = result.get("fill_price", entry_price)
                                        fill_size = result.get("fill_size", position_size)
                                        
                                        # Calculate entry fee
                                        entry_fee = calculate_polymarket_fee(fill_price, fill_size)
                                        entry_fee_cents = calculate_fee_cents(fill_price)
                                        
                                        cost = balance_tracker.buy(fill_price, fill_size, fee=entry_fee)
                                        entry_record = collector.record_fill("ENTRY", "DOWN", "BUY", fill_price, fill_size, down_book)
                                        
                                        # Create Excel trade record
                                        trade_record = excel_log.create_entry(
                                            window_slug=market['slug'],
                                            side="DOWN",
                                            entry_price=fill_price,
                                            entry_bid=down_book.best_bid,
                                            entry_ask=down_book.best_ask,
                                            entry_fee=entry_fee,
                                            position_size=fill_size,
                                            position_cost=cost,
                                            balance_before=balance_before
                                        )
                                        
                                        # Get real balance before entry for live mode PnL calculation
                                        if not DRY_RUN:
                                            balance_at_entry = client.get_balance()
                                        else:
                                            balance_at_entry = balance_before
                                        
                                        down_position = Position(
                                            token_id=down_token,
                                            side="DOWN",
                                            size=fill_size,
                                            entry_price=fill_price,
                                            entry_time=time.time(),
                                            entry_record=entry_record,
                                            entry_fee=entry_fee,
                                            trade_record=trade_record,
                                            balance_at_entry=balance_at_entry,
                                            buy_order_id=result.get("order_id", "")
                                        )
                                        
                                        total_trades += 1
                                        logger.log(f"[DOWN FILL] BUY {fill_size:.1f} @ {fill_price:.4f}, Cost: ${cost:.2f}")
                                        logger.log(f"[DOWN FILL] Entry fee: ${entry_fee:.4f} ({entry_fee_cents:.2f}c/contract)")
                                        logger.log(f"[DOWN FILL] Capital used: {CAPITAL_PERCENTAGE*100:.0f}% of ${balance_before:.2f} = ${capital_to_use:.2f}")
                                        
                                        logger.log(f"[DOWN] Waiting for TP@{TP_THRESHOLD} or SL@{SL_THRESHOLD}...")
                                        logger.raw(f"{'='*80}\n")
                                
                                # DOWN EXIT: Check TP or SL
                                if down_position is not None:
                                    down_mid = down_book.mid
                                    down_exit_type = None
                                    down_exit_price = 0.0
                                    
                                    if down_mid >= TP_THRESHOLD:
                                        down_exit_type = "TP"
                                        down_exit_price = down_book.best_bid
                                        logger.raw(f"\n{'='*80}")
                                        logger.log(f"[DOWN TP HIT] mid={down_mid:.2f} >= {TP_THRESHOLD}")
                                    elif down_mid <= SL_THRESHOLD:
                                        down_exit_type = "SL"
                                        down_exit_price = down_book.best_bid
                                        logger.raw(f"\n{'='*80}")
                                        logger.log(f"[DOWN SL HIT] mid={down_mid:.2f} <= {SL_THRESHOLD}")
                                    
                                    if down_exit_type:
                                        # CRITICAL: Validate position size before selling
                                        sell_size = down_position.size
                                        if sell_size <= 0:
                                            logger.log(f"[DOWN {down_exit_type}] ERROR: Invalid position size {sell_size}, resetting position")
                                            down_position = None
                                            continue
                                        
                                        # Round for clean sell (sell exactly what we have)
                                        sell_size = round(sell_size, 1)
                                        logger.log(f"[DOWN {down_exit_type}] Selling {sell_size} shares @ {down_exit_price:.2f}")
                                        
                                        # Keep retrying until sell succeeds
                                        sell_success = False
                                        sell_attempt = 0
                                        max_sell_attempts = 10  # Try up to 10 times
                                        
                                        while not sell_success and sell_attempt < max_sell_attempts:
                                            sell_attempt += 1
                                            
                                            # Check if window is ending - abandon if < 5 seconds left
                                            secs_left_now = market_end - int(time.time())
                                            if secs_left_now < 5:
                                                logger.log(f"[DOWN {down_exit_type}] TIMEOUT - Window ending in {secs_left_now}s, abandoning sell")
                                                break
                                            
                                            # Refresh price on retry
                                            if sell_attempt > 1:
                                                down_exit_price = down_book.best_bid
                                                logger.log(f"[DOWN {down_exit_type}] Retry #{sell_attempt} at bid={down_exit_price:.2f}")
                                                await asyncio.sleep(0.5)  # Brief pause before retry
                                            
                                            result = client.sell_market(down_position.token_id, down_exit_price, sell_size, buy_order_id=down_position.buy_order_id)
                                            
                                            if result.get("success"):
                                                sell_success = True
                                            else:
                                                error = result.get("error", "Unknown error")
                                                logger.log(f"[DOWN {down_exit_type}] SELL FAILED (attempt {sell_attempt}): {error}")
                                        
                                        if not sell_success:
                                            logger.log(f"[DOWN {down_exit_type}] ABANDONED - Could not sell, position lost to settlement!")
                                            down_position = None
                                            down_tp_hit = True  # Lock out to prevent new entries
                                            continue  # Move on to next window
                                        
                                        if result.get("success"):
                                            fill_price = result.get("fill_price", down_exit_price)
                                            logger.log(f"[DOWN {down_exit_type}] SELL {down_position.size:.1f} @ {fill_price:.4f}")
                                            
                                            # STEP 1: Wait 6 seconds for blockchain settlement (BOTH TP and SL)
                                            if not DRY_RUN:
                                                logger.log(f"[DOWN {down_exit_type}] Waiting 6s for blockchain settlement...")
                                                await asyncio.sleep(6.0)
                                                
                                                # STEP 2: Fetch REAL balance from chain
                                                balance_before_sell = down_position.balance_at_entry if hasattr(down_position, 'balance_at_entry') else balance_tracker.balance
                                                real_balance = client.get_balance()
                                                
                                                # STEP 3: Calculate PnL from REAL balance change
                                                pnl_dollars = real_balance - balance_before_sell
                                                
                                                # STEP 4: Update balance tracker with REAL balance
                                                balance_tracker.balance = real_balance
                                                
                                                logger.log(f"[DOWN {down_exit_type}] Balance before: ${balance_before_sell:.4f}")
                                                logger.log(f"[DOWN {down_exit_type}] Balance after:  ${real_balance:.4f}")
                                                logger.log(f"[DOWN {down_exit_type}] Real PnL: ${pnl_dollars:+.4f}")
                                            else:
                                                # Paper trading - use paper calculations
                                                exit_fee = calculate_polymarket_fee(fill_price, down_position.size)
                                                exit_fee_cents = calculate_fee_cents(fill_price)
                                                entry_fee_cents = calculate_fee_cents(down_position.entry_price)
                                                total_fees = down_position.entry_fee + exit_fee
                                                
                                                proceeds = balance_tracker.sell(fill_price, down_position.size, fee=exit_fee)
                                                pnl_dollars = (fill_price - down_position.entry_price) * down_position.size - total_fees
                                                
                                                gross_pnl_cents = (fill_price - down_position.entry_price) * 100
                                                net_pnl_cents = gross_pnl_cents - entry_fee_cents - exit_fee_cents
                                                logger.log(f"[DOWN {down_exit_type}] Gross: {gross_pnl_cents:+.1f}c, Fees: {entry_fee_cents + exit_fee_cents:.2f}c, Net: {net_pnl_cents:+.2f}c/contract")
                                            
                                            hold_time = time.time() - down_position.entry_time
                                            collector.record_fill(down_exit_type, "DOWN", "SELL", fill_price, down_position.size, down_book)
                                            collector.add_pnl(pnl_dollars * 100)  # Convert to cents for collector
                                            
                                            # STEP 5: Log to Excel
                                            if down_position.trade_record:
                                                exit_fee = calculate_polymarket_fee(fill_price, down_position.size) if DRY_RUN else 0
                                                gross_pnl_cents = (fill_price - down_position.entry_price) * 100
                                                net_pnl_cents = pnl_dollars * 100 / down_position.size if down_position.size > 0 else 0
                                                excel_log.complete_trade(
                                                    record=down_position.trade_record,
                                                    exit_type=down_exit_type,
                                                    exit_price=fill_price,
                                                    exit_bid=down_book.best_bid,
                                                    exit_ask=down_book.best_ask,
                                                    exit_fee=exit_fee,
                                                    exit_proceeds=fill_price * down_position.size,
                                                    gross_pnl_cents=gross_pnl_cents,
                                                    net_pnl_cents=net_pnl_cents,
                                                    net_pnl_dollars=pnl_dollars,
                                                    balance_after=balance_tracker.balance
                                                )
                                            
                                            logger.log(f"[DOWN {down_exit_type}] Total PnL: ${pnl_dollars:+.4f}, Hold: {hold_time:.1f}s")
                                            logger.log(f"[DOWN {down_exit_type}] Balance: ${balance_tracker.balance:.2f}, Win Rate: {excel_log.get_win_rate():.1f}%")
                                            
                                            # STEP 6: Set lock out flag (TP only)
                                            # Lock out after BOTH TP and SL
                                            down_tp_hit = True
                                            logger.log(f"[DOWN] LOCKED OUT for rest of window ({down_exit_type} hit)")
                                            if down_exit_type == "TP":
                                                logger.log(f"[DOWN TP] UP can now use {CAPITAL_PERCENTAGE*100:.0f}% of ${balance_tracker.balance:.2f} = ${balance_tracker.balance * CAPITAL_PERCENTAGE:.2f}")
                                            
                                            logger.raw(f"{'='*80}\n")
                                            down_position = None
                            
                            except asyncio.TimeoutError:
                                continue
                                
                except (websockets.exceptions.ConnectionClosedError, 
                        websockets.exceptions.ConnectionClosed,
                        ConnectionResetError,
                        OSError) as e:
                    reconnect_attempts += 1
                    logger.log(f"[WS] Connection lost: {type(e).__name__}")
                    
                    if reconnect_attempts < MAX_RECONNECT_ATTEMPTS:
                        pos_status = []
                        if up_position: pos_status.append("UP")
                        if down_position: pos_status.append("DOWN")
                        pos_str = f"positions: {', '.join(pos_status)}" if pos_status else "no positions"
                        logger.log(f"[WS] Retrying in {RECONNECT_DELAY_SECS}s... ({pos_str})")
                        await asyncio.sleep(RECONNECT_DELAY_SECS)
                    else:
                        logger.log(f"[WS] Max reconnect attempts reached, moving to next window")
                        
                except Exception as e:
                    reconnect_attempts += 1
                    logger.log(f"[ERROR] WebSocket: {e}")
                    import traceback
                    logger.raw(traceback.format_exc(), also_print=False)
                    
                    if reconnect_attempts < MAX_RECONNECT_ATTEMPTS:
                        await asyncio.sleep(RECONNECT_DELAY_SECS)
                    else:
                        logger.log(f"[WS] Max reconnect attempts reached, moving to next window")
            
            # Wait for next window to start
            next_slug, next_start, _ = get_next_window_slug()
            wait_time = next_start - time.time()
            if wait_time > 0 and time.time() < run_end_time:
                logger.log(f"[WAIT] {wait_time:.0f}s until next window: {next_slug}")
                await asyncio.sleep(min(wait_time + 2, 60))  # Wait + buffer
                    
    except KeyboardInterrupt:
        logger.log("\n[STOP] User interrupted")
    except Exception as e:
        logger.log(f"[ERROR] {e}")
        import traceback
        logger.raw(traceback.format_exc())
    finally:
        # WARNING: cancel_all() cancels ALL orders across ALL markets!
        # If running multiple scripts (BTC + ETH), don't use this
        # For now, keeping it for single-market safety on script exit
        if not DRY_RUN:
            logger.log("[CLEANUP] Cancelling orders for this market...")
            # Only cancel if we have tracked orders - safer for multi-market
            # client.cancel_all()  # DISABLED for multi-market safety
            pass
    
    # Print final balance summary
    balance_tracker.print_final_summary()
    
    # Print golden data summary
    collector.print_summary()
    
    # Print Excel trade log summary
    excel_log.print_summary()
    
    logger.raw(f"\n[DONE] Windows: {window_count}, Total Trades: {total_trades}")
    logger.raw(f"[LOG] Saved to: {log_filename}")
    logger.raw(f"[EXCEL] Saved to: {excel_filename}")
    
    logger.close()

# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    print("\n" + "="*80)
    print("  90c FLOW TEST - DUAL STATE MACHINE VERSION (OPTIMIZED)")
    print("="*80)
    print("  OPTIMIZED STRATEGY (from 50-day backtest):")
    print(f"    • Entry@{ENTRY_THRESHOLD*100:.0f}c in LAST {ENTRY_WINDOW_SECS // 60} MINUTES only")
    print(f"    • TP@{TP_THRESHOLD*100:.0f}c | SL@{SL_THRESHOLD*100:.0f}c")
    print("    • After SL or TP: Locked out for rest of window")
    print("    • UP and DOWN trade independently")
    print("")
    print("  CAPITAL MANAGEMENT:")
    print(f"    • Initial: ${INITIAL_BALANCE:.2f}")
    print(f"    • Position size: {CAPITAL_PERCENTAGE*100:.0f}% of balance (COMPOUNDING)")
    print("    • All trades logged to Excel with timestamps")
    print("="*80)
    print(f"\n  Usage:")
    print(f"    Dry run:  python test_90c_entry.py")
    print(f"    Live:     python test_90c_entry.py --live")
    print()
    
    asyncio.run(test_90c_flow_multiwindow())
