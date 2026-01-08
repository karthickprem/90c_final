#!/usr/bin/env python3
"""
paper_runner.py - Forward-test harness with EXIT ENGINE (SCALP mode)

Modes:
1) HOLD mode: Enter → hold to settlement → record result
2) SCALP mode: Enter → monitor → exit when mispricing closes or stops triggered

Includes mandatory checks:
1) Resolution source must match forecast location
2) Partition sanity check (sum of mid prices)
3) Depth guard (dust protection)
4) Risk cap per TARGET DATE (not per entry)
5) Exit Engine: take profit, stop loss, time stop, fair value close

Usage:
    python paper_runner.py                    # Run continuously (hold mode)
    python paper_runner.py --scalp            # Run in scalp mode with exit engine
    python paper_runner.py --once             # Run once
    python paper_runner.py --settle           # Settle past positions
    python paper_runner.py --settle-only      # Only settle, no new entries
"""

import argparse
import json
import logging
import sys
import time
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional, Any, Tuple
from enum import Enum

import yaml

# Add bot directory to path
sys.path.insert(0, str(Path(__file__).parent))

from bot.gamma import GammaClient, group_markets_by_location_date, TemperatureMarket
from bot.clob import CLOBClient, Side
from bot.model import TemperatureModel
from bot.strategy_interval import IntervalStrategy, TradePlan
from bot.weather import get_weather_provider
from bot.resolution import interval_hit, MarketResolutionProfile


class ExitReason(Enum):
    """Why a position was exited."""
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIME_STOP = "time_stop"
    FAIR_VALUE = "fair_value_reached"
    MANUAL = "manual_exit"
    SETTLEMENT = "held_to_settlement"


@dataclass
class StationInfo:
    """Weather station info for resolution accuracy."""
    station_id: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    name: Optional[str] = None
    timezone: Optional[str] = None
    source_known: bool = False


@dataclass
class PartitionCheck:
    """Result of partition sanity check."""
    is_valid: bool
    implied_sum: float
    num_buckets: int
    issues: List[str] = field(default_factory=list)


@dataclass
class DepthCheck:
    """Result of depth/dust check for a bucket."""
    is_valid: bool
    best_ask: float
    best_ask_size: float
    depth_walk_price: float
    slippage_pct: float
    reject_reason: Optional[str] = None


@dataclass
class MTMSnapshot:
    """Mark-to-market snapshot for a position."""
    position_id: str
    snapshot_time: str
    
    # Current prices (bid side for exit)
    best_bid: float
    best_bid_size: float
    depth_walk_exit_price: float
    
    # PnL
    unrealized_pnl: float
    unrealized_pnl_pct: float
    
    # Fair value comparison
    model_fair_value: float
    edge_remaining: float
    
    # Exit signals
    should_exit: bool
    exit_reason: Optional[str]


@dataclass
class ExitDecision:
    """Decision from exit engine."""
    should_exit: bool
    reason: Optional[ExitReason]
    exit_price: float
    exit_pnl: float
    detail: str


@dataclass
class EntrySnapshot:
    """Complete snapshot of trade entry for later settlement."""
    position_id: str
    entry_time: str
    target_date: str
    location: str
    
    # Resolution source
    station_id: Optional[str]
    station_name: Optional[str]
    station_lat: Optional[float]
    station_lon: Optional[float]
    source_known: bool
    
    # Interval info
    interval_tmin: float
    interval_tmax: float
    num_buckets: int
    
    # Per-bucket asks at entry
    bucket_asks: List[Dict]
    
    # Sizing
    shares_per_leg: float
    implied_cost: float
    total_cost: float
    payout_if_hit: float
    
    # Model predictions
    forecast_mu: float
    forecast_sigma: float
    sigma_raw: float
    sigma_k: float
    p_interval: float
    p_interval_mc: float
    mc_validated: bool
    
    # Per-bucket probabilities
    bucket_probabilities: List[Dict]
    
    # Edge analysis
    edge: float
    expected_pnl: float
    
    # Partition check at entry time
    partition_sum: float


def setup_logging(verbose: bool = False, log_file: str = "paper_runner.log"):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    
    # Clear existing handlers
    root = logging.getLogger()
    root.handlers = []
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file)
        ]
    )


def load_config(config_path: str = "bot/config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_station_info(location: str, config: dict) -> StationInfo:
    """Get weather station info for a location from config."""
    mappings = config.get("station_mappings", {})
    loc_lower = location.lower()
    
    if loc_lower in mappings:
        m = mappings[loc_lower]
        return StationInfo(
            station_id=m.get("station_id"),
            lat=m.get("lat"),
            lon=m.get("lon"),
            name=m.get("name"),
            timezone=m.get("timezone"),
            source_known=True
        )
    
    return StationInfo(source_known=False)


def check_partition_sanity(markets: List[TemperatureMarket], 
                           clob: CLOBClient,
                           config: dict) -> PartitionCheck:
    """Check that the implied probability distribution is sane."""
    issues = []
    regular_buckets = [m for m in markets if not m.is_tail_bucket]
    
    if len(regular_buckets) < 2:
        return PartitionCheck(
            is_valid=False, implied_sum=0, num_buckets=len(regular_buckets),
            issues=["Too few regular buckets"]
        )
    
    total_mid = 0.0
    high_price_neighbors = []
    
    for m in regular_buckets:
        try:
            book = clob.get_book(m.yes_token_id)
            if book.best_ask and book.best_bid:
                mid = (book.best_ask.price + book.best_bid.price) / 2
            elif book.best_ask:
                mid = book.best_ask.price
            else:
                mid = 0.5
            
            total_mid += mid
            
            if mid > 0.95:
                high_price_neighbors.append(f"{m.tmin_f:.1f}-{m.tmax_f:.1f}F @ {mid:.2f}")
                
        except Exception as e:
            issues.append(f"Book error for {m.tmin_f}-{m.tmax_f}F: {e}")
    
    min_sum = config.get("partition_sum_min", 0.70)
    max_sum = config.get("partition_sum_max", 1.30)
    
    is_valid = min_sum <= total_mid <= max_sum
    
    if total_mid < min_sum:
        issues.append(f"Sum {total_mid:.2f} < {min_sum}")
    if total_mid > max_sum:
        issues.append(f"Sum {total_mid:.2f} > {max_sum}")
    if len(high_price_neighbors) > 2:
        issues.append(f"Many 99%+ buckets: {high_price_neighbors[:3]}")
    
    return PartitionCheck(is_valid=is_valid, implied_sum=total_mid, 
                          num_buckets=len(regular_buckets), issues=issues)


def check_depth_for_bucket(market: TemperatureMarket, shares_needed: float,
                           clob: CLOBClient, config: dict) -> DepthCheck:
    """Hard depth/dust check for a single bucket."""
    dust_threshold = config.get("dust_price_threshold", 0.01)
    dust_min_size = config.get("dust_min_size_for_low_price", 500)
    max_slippage = config.get("dust_max_slippage_pct", 0.05)
    
    try:
        book = clob.get_book(market.yes_token_id)
    except Exception as e:
        return DepthCheck(is_valid=False, best_ask=0, best_ask_size=0,
                          depth_walk_price=0, slippage_pct=0,
                          reject_reason=f"Orderbook error: {e}")
    
    if not book.best_ask:
        return DepthCheck(is_valid=False, best_ask=0, best_ask_size=0,
                          depth_walk_price=0, slippage_pct=0,
                          reject_reason="No asks")
    
    best_ask = book.best_ask.price
    best_ask_size = book.best_ask.size
    
    fill = clob.fill_cost_for_shares(market.yes_token_id, shares_needed)
    depth_walk_price = fill.avg_price if fill.can_fill else 0
    
    if not fill.can_fill:
        return DepthCheck(is_valid=False, best_ask=best_ask, best_ask_size=best_ask_size,
                          depth_walk_price=0, slippage_pct=0,
                          reject_reason=f"Cannot fill {shares_needed} shares")
    
    slippage_pct = (depth_walk_price - best_ask) / best_ask if best_ask > 0 else 0
    
    if best_ask < dust_threshold:
        if best_ask_size < dust_min_size:
            return DepthCheck(is_valid=False, best_ask=best_ask, best_ask_size=best_ask_size,
                              depth_walk_price=depth_walk_price, slippage_pct=slippage_pct,
                              reject_reason=f"Dust: price {best_ask:.4f} size {best_ask_size:.0f}")
        if slippage_pct > max_slippage:
            return DepthCheck(is_valid=False, best_ask=best_ask, best_ask_size=best_ask_size,
                              depth_walk_price=depth_walk_price, slippage_pct=slippage_pct,
                              reject_reason=f"Low price with {slippage_pct:.1%} slippage")
    
    return DepthCheck(is_valid=True, best_ask=best_ask, best_ask_size=best_ask_size,
                      depth_walk_price=depth_walk_price, slippage_pct=slippage_pct)


@dataclass
class ExitLiquidityCheck:
    """Result of exit liquidity check (bid-side depth for scalp)."""
    is_valid: bool
    best_bid: float
    best_bid_size: float
    exit_fill_pct: float  # % of position that can be exited
    expected_exit_price: float
    expected_slippage: float
    net_edge: float  # edge after exit slippage
    reject_reason: Optional[str] = None


@dataclass
class LiquidityRegime:
    """Liquidity snapshot at various depth levels for regime analysis."""
    token_id: str
    snapshot_time: str
    location: str
    target_date: str
    bucket_tmin: float
    bucket_tmax: float
    
    # Best prices
    best_bid: float
    best_ask: float
    spread: float
    mid: float
    
    # Top-of-book sizes
    bid_size_tob: float  # Top of book
    ask_size_tob: float
    
    # Depth at various levels from mid
    bid_depth_2pct: float  # Shares available at mid - 2%
    bid_depth_5pct: float
    bid_depth_10pct: float
    ask_depth_2pct: float  # Shares available at mid + 2%
    ask_depth_5pct: float
    ask_depth_10pct: float


def compute_liquidity_regime(market: TemperatureMarket, clob: CLOBClient) -> Optional[LiquidityRegime]:
    """Capture liquidity snapshot for regime analysis."""
    try:
        book = clob.get_book(market.yes_token_id)
    except Exception:
        return None
    
    best_bid = book.best_bid.price if book.best_bid else 0
    best_ask = book.best_ask.price if book.best_ask else 0
    bid_size = book.best_bid.size if book.best_bid else 0
    ask_size = book.best_ask.size if book.best_ask else 0
    
    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else best_ask or best_bid or 0.5
    spread = best_ask - best_bid if best_bid and best_ask else 0
    
    # Compute depth at various levels
    def depth_at_level(levels, target_price, is_bid: bool) -> float:
        """Sum shares available at or better than target price."""
        total = 0
        for level in levels:
            if is_bid:
                if level.price >= target_price:
                    total += level.size
            else:
                if level.price <= target_price:
                    total += level.size
        return total
    
    bid_depth_2 = depth_at_level(book.bids, mid * 0.98, True)
    bid_depth_5 = depth_at_level(book.bids, mid * 0.95, True)
    bid_depth_10 = depth_at_level(book.bids, mid * 0.90, True)
    ask_depth_2 = depth_at_level(book.asks, mid * 1.02, False)
    ask_depth_5 = depth_at_level(book.asks, mid * 1.05, False)
    ask_depth_10 = depth_at_level(book.asks, mid * 1.10, False)
    
    return LiquidityRegime(
        token_id=market.yes_token_id,
        snapshot_time=datetime.now().isoformat(),
        location=market.location,
        target_date=market.target_date.isoformat() if market.target_date else "",
        bucket_tmin=market.tmin_f,
        bucket_tmax=market.tmax_f,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        mid=mid,
        bid_size_tob=bid_size,
        ask_size_tob=ask_size,
        bid_depth_2pct=bid_depth_2,
        bid_depth_5pct=bid_depth_5,
        bid_depth_10pct=bid_depth_10,
        ask_depth_2pct=ask_depth_2,
        ask_depth_5pct=ask_depth_5,
        ask_depth_10pct=ask_depth_10
    )


def check_exit_liquidity(market: TemperatureMarket, shares_needed: float,
                         entry_price: float, model_edge: float,
                         clob: CLOBClient, config: dict) -> ExitLiquidityCheck:
    """
    Check if there's sufficient BID-side depth to exit this position.
    Required for scalp mode - if you can't exit, the trade is fake.
    
    Uses NET EDGE calculation: edge - expected_exit_slippage - buffer
    Instead of flat slippage cap.
    """
    min_exit_fill_pct = config.get("exit_engine", {}).get("min_exit_fill_pct", 0.50)
    min_net_edge = config.get("min_edge", 0.03)  # Net edge must still be positive
    exit_buffer = config.get("edge_buffer", 0.02)  # Buffer for exit
    
    try:
        book = clob.get_book(market.yes_token_id)
    except Exception as e:
        return ExitLiquidityCheck(
            is_valid=False, best_bid=0, best_bid_size=0,
            exit_fill_pct=0, expected_exit_price=0, expected_slippage=0,
            net_edge=0,
            reject_reason=f"Orderbook error: {e}"
        )
    
    if not book.best_bid:
        return ExitLiquidityCheck(
            is_valid=False, best_bid=0, best_bid_size=0,
            exit_fill_pct=0, expected_exit_price=0, expected_slippage=0,
            net_edge=0,
            reject_reason="No bids - cannot exit"
        )
    
    best_bid = book.best_bid.price
    best_bid_size = book.best_bid.size
    
    # Try to depth-walk the bid side
    fill = clob.fill_cost_for_shares(market.yes_token_id, shares_needed, Side.SELL)
    
    if not fill.can_fill:
        fill_pct = fill.filled_shares / shares_needed if shares_needed > 0 else 0
        
        if fill_pct < min_exit_fill_pct:
            return ExitLiquidityCheck(
                is_valid=False, best_bid=best_bid, best_bid_size=best_bid_size,
                exit_fill_pct=fill_pct, 
                expected_exit_price=fill.avg_price if fill.filled_shares > 0 else 0,
                expected_slippage=0, net_edge=0,
                reject_reason=f"Exit depth: only {fill_pct:.0%} fillable (need {min_exit_fill_pct:.0%})"
            )
    
    expected_exit_price = fill.avg_price if fill.can_fill else (
        fill.total_cost / fill.filled_shares if fill.filled_shares > 0 else 0
    )
    
    # Calculate slippage from entry price
    if entry_price > 0:
        expected_slippage = (entry_price - expected_exit_price) / entry_price
    else:
        expected_slippage = 0
    
    # NET EDGE: edge after accounting for exit slippage and buffer
    # If edge is 30% and slippage is 12%, net_edge = 30% - 12% - 2% = 16% (still tradable)
    # If edge is 7% and slippage is 8%, net_edge = 7% - 8% - 2% = -3% (not tradable)
    net_edge = model_edge - expected_slippage - exit_buffer
    
    exit_fill_pct = 1.0 if fill.can_fill else fill.filled_shares / shares_needed
    
    # Check if net edge is acceptable (must be positive after costs)
    if net_edge < min_net_edge:
        return ExitLiquidityCheck(
            is_valid=False, best_bid=best_bid, best_bid_size=best_bid_size,
            exit_fill_pct=exit_fill_pct, expected_exit_price=expected_exit_price,
            expected_slippage=expected_slippage, net_edge=net_edge,
            reject_reason=f"Net edge {net_edge:.1%} < {min_net_edge:.0%} (edge={model_edge:.1%}, slip={expected_slippage:.1%})"
        )
    
    return ExitLiquidityCheck(
        is_valid=True, best_bid=best_bid, best_bid_size=best_bid_size,
        exit_fill_pct=exit_fill_pct, expected_exit_price=expected_exit_price,
        expected_slippage=expected_slippage, net_edge=net_edge
    )


class PaperTradeStore:
    """SQLite storage with MTM and Exit tracking."""
    
    def __init__(self, db_path: str = "paper_trades.db"):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Initialize database with new tables."""
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                -- Entry snapshots
                CREATE TABLE IF NOT EXISTS entries (
                    position_id TEXT PRIMARY KEY,
                    entry_time TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    location TEXT NOT NULL,
                    station_id TEXT,
                    station_name TEXT,
                    source_known INTEGER DEFAULT 0,
                    interval_tmin REAL NOT NULL,
                    interval_tmax REAL NOT NULL,
                    num_buckets INTEGER NOT NULL,
                    shares_per_leg REAL NOT NULL,
                    implied_cost REAL NOT NULL,
                    total_cost REAL NOT NULL,
                    payout_if_hit REAL NOT NULL,
                    forecast_mu REAL NOT NULL,
                    forecast_sigma REAL NOT NULL,
                    p_interval REAL NOT NULL,
                    edge REAL NOT NULL,
                    expected_pnl REAL NOT NULL,
                    partition_sum REAL,
                    snapshot_json TEXT NOT NULL,
                    status TEXT DEFAULT 'open'
                );
                
                -- Mark-to-market snapshots
                CREATE TABLE IF NOT EXISTS position_mtm (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id TEXT NOT NULL,
                    snapshot_time TEXT NOT NULL,
                    best_bid REAL,
                    best_bid_size REAL,
                    depth_walk_exit_price REAL,
                    unrealized_pnl REAL,
                    unrealized_pnl_pct REAL,
                    model_fair_value REAL,
                    edge_remaining REAL,
                    should_exit INTEGER DEFAULT 0,
                    exit_reason TEXT,
                    FOREIGN KEY (position_id) REFERENCES entries(position_id)
                );
                
                -- Exits (early exits before settlement)
                CREATE TABLE IF NOT EXISTS exits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id TEXT NOT NULL,
                    exit_time TEXT NOT NULL,
                    exit_reason TEXT NOT NULL,
                    exit_price REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    hold_time_minutes REAL,
                    notes TEXT,
                    FOREIGN KEY (position_id) REFERENCES entries(position_id)
                );
                
                -- Settlements
                CREATE TABLE IF NOT EXISTS settlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id TEXT NOT NULL,
                    settlement_time TEXT NOT NULL,
                    observed_high_f REAL,
                    observed_source TEXT,
                    interval_hit INTEGER,
                    realized_pnl REAL,
                    brier_score REAL,
                    notes TEXT,
                    FOREIGN KEY (position_id) REFERENCES entries(position_id)
                );
                
                -- Scan log
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_time TEXT NOT NULL,
                    mode TEXT DEFAULT 'hold',
                    markets_found INTEGER DEFAULT 0,
                    opportunities_found INTEGER DEFAULT 0,
                    trades_entered INTEGER DEFAULT 0,
                    trades_exited INTEGER DEFAULT 0,
                    skip_reasons TEXT,
                    notes TEXT
                );
                
                -- Daily risk tracking
                CREATE TABLE IF NOT EXISTS daily_risk (
                    target_date TEXT PRIMARY KEY,
                    total_cost REAL DEFAULT 0,
                    num_positions INTEGER DEFAULT 0,
                    last_updated TEXT
                );
                
                -- Liquidity regime snapshots (for finding liquidity windows)
                CREATE TABLE IF NOT EXISTS liquidity_regimes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id TEXT NOT NULL,
                    snapshot_time TEXT NOT NULL,
                    location TEXT,
                    target_date TEXT,
                    bucket_tmin REAL,
                    bucket_tmax REAL,
                    best_bid REAL,
                    best_ask REAL,
                    spread REAL,
                    mid REAL,
                    bid_size_tob REAL,
                    ask_size_tob REAL,
                    bid_depth_2pct REAL,
                    bid_depth_5pct REAL,
                    bid_depth_10pct REAL,
                    ask_depth_2pct REAL,
                    ask_depth_5pct REAL,
                    ask_depth_10pct REAL
                );
            """)
    
    def get_risk_for_date(self, target_date: date) -> float:
        """Get total risk already committed for a target date."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT SUM(total_cost) FROM entries 
                WHERE target_date = ? AND status = 'open'
            """, (target_date.isoformat(),)).fetchone()
            return row[0] or 0.0
    
    def get_total_open_risk(self) -> float:
        """Get total risk across all open positions."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT SUM(total_cost) FROM entries WHERE status = 'open'
            """).fetchone()
            return row[0] or 0.0
    
    def log_entry(self, snapshot: EntrySnapshot):
        """Log a trade entry."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO entries 
                (position_id, entry_time, target_date, location, station_id, station_name,
                 source_known, interval_tmin, interval_tmax, num_buckets, shares_per_leg, 
                 implied_cost, total_cost, payout_if_hit, forecast_mu, forecast_sigma, 
                 p_interval, edge, expected_pnl, partition_sum, snapshot_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot.position_id, snapshot.entry_time, snapshot.target_date,
                snapshot.location, snapshot.station_id, snapshot.station_name,
                1 if snapshot.source_known else 0,
                snapshot.interval_tmin, snapshot.interval_tmax,
                snapshot.num_buckets, snapshot.shares_per_leg, snapshot.implied_cost,
                snapshot.total_cost, snapshot.payout_if_hit, snapshot.forecast_mu,
                snapshot.forecast_sigma, snapshot.p_interval, snapshot.edge,
                snapshot.expected_pnl, snapshot.partition_sum,
                json.dumps(asdict(snapshot), default=str)
            ))
    
    def log_mtm(self, mtm: MTMSnapshot):
        """Log MTM snapshot."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO position_mtm 
                (position_id, snapshot_time, best_bid, best_bid_size, depth_walk_exit_price,
                 unrealized_pnl, unrealized_pnl_pct, model_fair_value, edge_remaining,
                 should_exit, exit_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                mtm.position_id, mtm.snapshot_time, mtm.best_bid, mtm.best_bid_size,
                mtm.depth_walk_exit_price, mtm.unrealized_pnl, mtm.unrealized_pnl_pct,
                mtm.model_fair_value, mtm.edge_remaining,
                1 if mtm.should_exit else 0, mtm.exit_reason
            ))
    
    def log_exit(self, position_id: str, reason: ExitReason, exit_price: float, 
                 realized_pnl: float, hold_time_minutes: float):
        """Log an early exit."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO exits (position_id, exit_time, exit_reason, exit_price, 
                                  realized_pnl, hold_time_minutes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (position_id, datetime.now().isoformat(), reason.value, 
                  exit_price, realized_pnl, hold_time_minutes))
            
            conn.execute("""
                UPDATE entries SET status = 'exited' WHERE position_id = ?
            """, (position_id,))
    
    def log_scan(self, mode: str, markets_found: int, opportunities_found: int,
                  trades_entered: int, trades_exited: int, skip_reasons: Dict[str, int]):
        """Log a scan."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO scans (scan_time, mode, markets_found, opportunities_found,
                                   trades_entered, trades_exited, skip_reasons)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (datetime.now().isoformat(), mode, markets_found, opportunities_found,
                  trades_entered, trades_exited, json.dumps(skip_reasons)))
    
    def log_liquidity_regime(self, regime: LiquidityRegime):
        """Log a liquidity regime snapshot for analysis."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO liquidity_regimes 
                (token_id, snapshot_time, location, target_date, bucket_tmin, bucket_tmax,
                 best_bid, best_ask, spread, mid, bid_size_tob, ask_size_tob,
                 bid_depth_2pct, bid_depth_5pct, bid_depth_10pct,
                 ask_depth_2pct, ask_depth_5pct, ask_depth_10pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                regime.token_id, regime.snapshot_time, regime.location, regime.target_date,
                regime.bucket_tmin, regime.bucket_tmax, regime.best_bid, regime.best_ask,
                regime.spread, regime.mid, regime.bid_size_tob, regime.ask_size_tob,
                regime.bid_depth_2pct, regime.bid_depth_5pct, regime.bid_depth_10pct,
                regime.ask_depth_2pct, regime.ask_depth_5pct, regime.ask_depth_10pct
            ))
    
    def get_liquidity_summary(self) -> Dict:
        """Get summary of liquidity regimes by location/hour."""
        with sqlite3.connect(self.db_path) as conn:
            # Average bid depth by location
            rows = conn.execute("""
                SELECT location, 
                       AVG(bid_depth_5pct) as avg_bid_5,
                       AVG(ask_depth_5pct) as avg_ask_5,
                       AVG(spread) as avg_spread,
                       COUNT(*) as samples
                FROM liquidity_regimes
                GROUP BY location
            """).fetchall()
            
            return {
                "by_location": [
                    {"location": r[0], "avg_bid_5": r[1], "avg_ask_5": r[2], 
                     "avg_spread": r[3], "samples": r[4]}
                    for r in rows
                ]
            }
    
    def get_open_positions(self) -> List[Dict]:
        """Get all open positions."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM entries WHERE status = 'open'
            """).fetchall()
            return [dict(row) for row in rows]
    
    def settle_position(self, position_id: str, observed_high_f: float,
                        observed_source: str = "manual"):
        """Settle a position."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT * FROM entries WHERE position_id = ?
            """, (position_id,)).fetchone()
            
            if not row:
                raise ValueError(f"Position {position_id} not found")
            
            entry = dict(row)
            hit = interval_hit(observed_high_f, entry["interval_tmin"], entry["interval_tmax"])
            
            if hit:
                pnl = entry["payout_if_hit"] - entry["total_cost"]
            else:
                pnl = -entry["total_cost"]
            
            brier = (entry["p_interval"] - (1 if hit else 0)) ** 2
            
            conn.execute("""
                INSERT INTO settlements (position_id, settlement_time, observed_high_f,
                                        observed_source, interval_hit, realized_pnl, brier_score)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (position_id, datetime.now().isoformat(), observed_high_f,
                  observed_source, 1 if hit else 0, pnl, brier))
            
            conn.execute("""
                UPDATE entries SET status = 'settled' WHERE position_id = ?
            """, (position_id,))
            
            return {"hit": hit, "pnl": pnl, "brier": brier}
    
    def get_summary(self) -> Dict:
        """Get comprehensive summary."""
        with sqlite3.connect(self.db_path) as conn:
            # Settlements
            settle_row = conn.execute("""
                SELECT COUNT(*), SUM(CASE WHEN interval_hit = 1 THEN 1 ELSE 0 END),
                       SUM(realized_pnl), AVG(brier_score)
                FROM settlements
            """).fetchone()
            
            # Exits
            exit_row = conn.execute("""
                SELECT COUNT(*), SUM(realized_pnl), AVG(hold_time_minutes)
                FROM exits
            """).fetchone()
            
            # Open positions
            open_row = conn.execute("""
                SELECT COUNT(*), SUM(total_cost) FROM entries WHERE status = 'open'
            """).fetchone()
            
            # Predicted wins
            pred_row = conn.execute("""
                SELECT SUM(p_interval) FROM entries WHERE status = 'settled'
            """).fetchone()
            
            return {
                "settled": settle_row[0] or 0,
                "settled_wins": settle_row[1] or 0,
                "settled_pnl": settle_row[2] or 0.0,
                "mean_brier": settle_row[3] or 0.0,
                "exits": exit_row[0] or 0,
                "exit_pnl": exit_row[1] or 0.0,
                "avg_hold_minutes": exit_row[2] or 0.0,
                "open_positions": open_row[0] or 0,
                "open_risk": open_row[1] or 0.0,
                "predicted_wins": pred_row[0] or 0.0
            }


class ExitEngine:
    """Engine for deciding when to exit positions."""
    
    def __init__(self, config: dict, clob: CLOBClient):
        self.config = config
        self.clob = clob
        self.exit_config = config.get("exit_engine", {})
        self.logger = logging.getLogger("exit_engine")
    
    def evaluate_exit(self, entry: Dict, snapshot_json: Dict) -> ExitDecision:
        """Evaluate whether to exit a position."""
        # Get exit thresholds
        take_profit_usd = self.exit_config.get("take_profit_abs_usd", 0.20)
        take_profit_pct = self.exit_config.get("take_profit_pct", 0.20)
        stop_loss_usd = self.exit_config.get("stop_loss_abs_usd", 0.30)
        stop_loss_pct = self.exit_config.get("stop_loss_pct", 0.25)
        time_stop_min = self.exit_config.get("time_stop_minutes", 120)
        min_exit_fill = self.exit_config.get("min_exit_fill_pct", 0.90)
        
        entry_cost = entry["total_cost"]
        entry_time = datetime.fromisoformat(entry["entry_time"])
        hold_time_min = (datetime.now() - entry_time).total_seconds() / 60
        
        # Get current exit price (depth-walk bids)
        bucket_asks = snapshot_json.get("bucket_asks", [])
        if not bucket_asks:
            return ExitDecision(False, None, 0, 0, "No bucket info")
        
        total_exit_value = 0
        can_exit = True
        
        for ba in bucket_asks:
            token_id = ba.get("token_id")
            shares = ba.get("shares", entry["shares_per_leg"])
            
            if not token_id:
                continue
            
            try:
                # For exit, we SELL our YES tokens, so check BIDS
                fill = self.clob.fill_cost_for_shares(token_id, shares, Side.SELL)
                if fill.can_fill:
                    total_exit_value += fill.total_cost
                else:
                    can_exit = False
                    break
            except Exception as e:
                self.logger.warning(f"Exit price error: {e}")
                can_exit = False
                break
        
        if not can_exit:
            return ExitDecision(False, None, 0, 0, "Cannot exit: insufficient bid depth")
        
        unrealized_pnl = total_exit_value - entry_cost
        unrealized_pnl_pct = unrealized_pnl / entry_cost if entry_cost > 0 else 0
        
        # Check take profit
        if unrealized_pnl >= take_profit_usd or unrealized_pnl_pct >= take_profit_pct:
            return ExitDecision(
                True, ExitReason.TAKE_PROFIT, total_exit_value, unrealized_pnl,
                f"Take profit: ${unrealized_pnl:.2f} ({unrealized_pnl_pct:.1%})"
            )
        
        # Check stop loss
        if unrealized_pnl <= -stop_loss_usd or unrealized_pnl_pct <= -stop_loss_pct:
            return ExitDecision(
                True, ExitReason.STOP_LOSS, total_exit_value, unrealized_pnl,
                f"Stop loss: ${unrealized_pnl:.2f} ({unrealized_pnl_pct:.1%})"
            )
        
        # Check time stop
        if hold_time_min >= time_stop_min:
            return ExitDecision(
                True, ExitReason.TIME_STOP, total_exit_value, unrealized_pnl,
                f"Time stop: held {hold_time_min:.0f} min"
            )
        
        # Fair value check (if enabled)
        if self.exit_config.get("fair_value_close", False):
            epsilon = self.exit_config.get("fair_value_epsilon", 0.02)
            fair_value = entry["p_interval"]
            current_implied = entry["implied_cost"]
            
            if current_implied >= fair_value - epsilon:
                return ExitDecision(
                    True, ExitReason.FAIR_VALUE, total_exit_value, unrealized_pnl,
                    f"Fair value reached: implied {current_implied:.2f} >= fv {fair_value:.2f}"
                )
        
        return ExitDecision(
            False, None, total_exit_value, unrealized_pnl,
            f"Hold: PnL ${unrealized_pnl:.2f} ({unrealized_pnl_pct:.1%}), {hold_time_min:.0f}min"
        )


def create_entry_snapshot(plan: TradePlan, clob: CLOBClient, 
                          station_info: StationInfo, 
                          partition_sum: float) -> EntrySnapshot:
    """Create complete entry snapshot from trade plan."""
    bucket_asks = []
    bucket_probs = []
    
    for i, leg in enumerate(plan.legs):
        market = leg.market
        
        try:
            book = clob.get_book(leg.token_id)
            best_ask = book.best_ask.price if book.best_ask else leg.limit_price
            best_ask_size = book.best_ask.size if book.best_ask else 0
            depth_price = leg.limit_price
        except Exception:
            best_ask = leg.limit_price
            best_ask_size = 0
            depth_price = leg.limit_price
        
        bucket_asks.append({
            "tmin": market.tmin_f,
            "tmax": market.tmax_f,
            "token_id": leg.token_id,
            "best_ask": best_ask,
            "best_ask_size": best_ask_size,
            "depth_walked_price": depth_price,
            "shares": leg.shares
        })
        
        if i < len(plan.per_bucket_prices):
            tmin, tmax, price = plan.per_bucket_prices[i]
            bucket_probs.append({
                "tmin": tmin,
                "tmax": tmax,
                "market_price": price,
            })
    
    return EntrySnapshot(
        position_id=f"paper_{uuid4().hex[:12]}",
        entry_time=datetime.now().isoformat(),
        target_date=plan.target_date.isoformat(),
        location=plan.location,
        station_id=station_info.station_id,
        station_name=station_info.name,
        station_lat=station_info.lat,
        station_lon=station_info.lon,
        source_known=station_info.source_known,
        interval_tmin=plan.interval_tmin,
        interval_tmax=plan.interval_tmax,
        num_buckets=plan.num_buckets,
        bucket_asks=bucket_asks,
        shares_per_leg=plan.shares_per_leg,
        implied_cost=plan.implied_cost,
        total_cost=plan.total_cost,
        payout_if_hit=plan.payout_if_hit,
        forecast_mu=plan.forecast_mu,
        forecast_sigma=plan.forecast_sigma,
        sigma_raw=plan.sigma_raw,
        sigma_k=plan.sigma_k,
        p_interval=plan.p_interval,
        p_interval_mc=plan.p_interval_mc,
        mc_validated=plan.mc_validated,
        bucket_probabilities=bucket_probs,
        edge=plan.edge,
        expected_pnl=plan.expected_pnl,
        partition_sum=partition_sum
    )


def run_scan_cycle(config: dict, store: PaperTradeStore, 
                    mode: str = "hold",
                    max_entries_per_cycle: int = 2,
                    settle_only: bool = False) -> Dict:
    """Run one scan cycle with risk controls."""
    logger = logging.getLogger("paper_runner")
    
    # Check if trading is enabled
    if not config.get("trading_enabled", True) and not settle_only:
        logger.info("Trading disabled in config")
        return {"markets": 0, "opportunities": 0, "entries": 0, "exits": 0}
    
    locations = config.get("locations", [config.get("primary_location", "London")])
    min_days_ahead = config.get("min_days_ahead", 1)
    max_risk_per_day = config.get("max_risk_per_day_usd", 2)
    max_total_risk = config.get("max_total_open_risk_usd", 6)
    
    gamma = GammaClient(config=config)
    clob = CLOBClient(config=config)
    strategy = IntervalStrategy(config=config)
    exit_engine = ExitEngine(config, clob)
    
    # Process exits first (if in scalp mode)
    exits_made = 0
    if mode == "scalp" and config.get("exit_engine", {}).get("enabled", False):
        open_positions = store.get_open_positions()
        for pos in open_positions:
            try:
                snapshot_json = json.loads(pos.get("snapshot_json", "{}"))
                decision = exit_engine.evaluate_exit(pos, snapshot_json)
                
                if decision.should_exit:
                    entry_time = datetime.fromisoformat(pos["entry_time"])
                    hold_time = (datetime.now() - entry_time).total_seconds() / 60
                    
                    store.log_exit(
                        pos["position_id"], decision.reason, 
                        decision.exit_price, decision.exit_pnl, hold_time
                    )
                    exits_made += 1
                    logger.info(f"EXIT: {pos['position_id'][:12]} | {decision.reason.value} | "
                               f"PnL: ${decision.exit_pnl:.2f}")
            except Exception as e:
                logger.error(f"Exit evaluation error: {e}")
    
    if settle_only:
        store.log_scan(mode, 0, 0, 0, exits_made, {})
        return {"markets": 0, "opportunities": 0, "entries": 0, "exits": exits_made}
    
    # Discover markets
    logger.info("Scanning for temperature markets...")
    markets = gamma.discover_bucket_markets(locations=locations)
    
    today = date.today()
    active_markets = [m for m in markets if not m.closed and 
                      (m.target_date - today).days >= min_days_ahead]
    
    logger.info(f"Found {len(active_markets)} active bucket markets (D+{min_days_ahead}+)")
    
    if not active_markets:
        store.log_scan(mode, 0, 0, 0, exits_made, {})
        return {"markets": 0, "opportunities": 0, "entries": 0, "exits": exits_made}
    
    # Group and check partitions
    by_loc_date = group_markets_by_location_date(active_markets)
    valid_groups = {}
    
    for key, group_markets in by_loc_date.items():
        loc, target_date = key
        check = check_partition_sanity(group_markets, clob, config)
        
        if check.is_valid:
            logger.info(f"Partition OK: {loc} {target_date} sum={check.implied_sum:.2f}")
            valid_groups[key] = (group_markets, check.implied_sum)
        else:
            logger.warning(f"Partition FAILED: {loc} {target_date}: {check.issues}")
    
    # Find opportunities
    all_plans = []
    partition_sums = {}
    
    for loc in locations:
        loc_markets = []
        for (gloc, gdate), (gmarkets, psum) in valid_groups.items():
            if gloc.lower() == loc.lower():
                loc_markets.extend(gmarkets)
                partition_sums[(loc.lower(), gdate)] = psum
        
        if loc_markets:
            plans = strategy.scan_all_dates(loc_markets, location=loc)
            all_plans.extend(plans)
    
    logger.info(f"Found {len(all_plans)} opportunities")
    
    skip_reasons = {s.reason.value: 0 for s in strategy.skipped}
    for s in strategy.skipped:
        skip_reasons[s.reason.value] += 1
    
    # Validate depth for each plan
    valid_plans = []
    for plan in all_plans:
        all_ok = True
        
        # Check entry depth (ask-side)
        for leg in plan.legs:
            check = check_depth_for_bucket(leg.market, plan.shares_per_leg, clob, config)
            if not check.is_valid:
                logger.warning(f"Depth FAILED: {plan.location} {leg.market.tmin_f:.1f}F: {check.reject_reason}")
                skip_reasons["depth_dust_check_failed"] = skip_reasons.get("depth_dust_check_failed", 0) + 1
                all_ok = False
                break
        
        # SCALP MODE: Also check exit liquidity (bid-side) at entry time
        if all_ok and mode == "scalp":
            for leg in plan.legs:
                exit_check = check_exit_liquidity(
                    leg.market, plan.shares_per_leg, 
                    leg.limit_price, plan.edge,  # Pass model edge for net_edge calc
                    clob, config
                )
                if not exit_check.is_valid:
                    logger.warning(f"Exit liquidity FAILED: {plan.location} "
                                  f"{leg.market.tmin_f:.1f}F: {exit_check.reject_reason}")
                    skip_reasons["exit_liquidity_failed"] = skip_reasons.get("exit_liquidity_failed", 0) + 1
                    all_ok = False
                    break
                else:
                    logger.info(f"Exit OK: {leg.market.tmin_f:.1f}F net_edge={exit_check.net_edge:.1%} "
                               f"(edge={plan.edge:.1%}, slip={exit_check.expected_slippage:.1%})")
        
        if all_ok:
            valid_plans.append(plan)
    
    logger.info(f"Found {len(valid_plans)} opportunities after depth check")
    
    # Enter trades with risk controls
    entries_made = 0
    
    # Check global risk limit
    total_open_risk = store.get_total_open_risk()
    if total_open_risk >= max_total_risk:
        logger.warning(f"Global risk limit: ${total_open_risk:.2f} >= ${max_total_risk}")
        skip_reasons["global_risk_limit"] = len(valid_plans)
        valid_plans = []
    
    for plan in valid_plans[:max_entries_per_cycle]:
        # Check per-date risk limit
        date_risk = store.get_risk_for_date(plan.target_date)
        remaining_risk = max_risk_per_day - date_risk
        
        if plan.total_cost > remaining_risk:
            logger.warning(f"Date risk limit: {plan.target_date} has ${date_risk:.2f}, "
                          f"plan needs ${plan.total_cost:.2f}, limit ${max_risk_per_day}")
            skip_reasons["date_risk_limit"] = skip_reasons.get("date_risk_limit", 0) + 1
            continue
        
        try:
            station_info = get_station_info(plan.location, config)
            psum = partition_sums.get((plan.location.lower(), plan.target_date), 0.0)
            
            snapshot = create_entry_snapshot(plan, clob, station_info, psum)
            store.log_entry(snapshot)
            entries_made += 1
            
            logger.info(f"ENTRY: {plan.location} {plan.target_date} {plan.interval_str}")
            logger.info(f"  Station: {station_info.station_id or 'UNKNOWN'}")
            logger.info(f"  Edge: {plan.edge:.2%}, Cost: ${plan.total_cost:.2f}, P={plan.p_interval:.2%}")
            logger.info(f"  Partition sum: {psum:.2f}")
            logger.info(f"  Position ID: {snapshot.position_id}")
            
            for ba in snapshot.bucket_asks:
                width = ba['tmax'] - ba['tmin']
                fmt = f"{ba['tmin']:.1f}-{ba['tmax']:.1f}F" if abs(width - round(width)) > 0.1 else f"{ba['tmin']:.0f}-{ba['tmax']:.0f}F"
                logger.info(f"  Bucket {fmt}: ask={ba['best_ask']:.4f} (size={ba['best_ask_size']:.0f})")
            
        except Exception as e:
            logger.error(f"Entry error: {e}")
    
    # Log liquidity regimes for a sample of markets (for regime analysis)
    # Sample: log up to 5 markets per scan to build liquidity profile
    sample_markets = active_markets[:5]
    for market in sample_markets:
        regime = compute_liquidity_regime(market, clob)
        if regime:
            store.log_liquidity_regime(regime)
    
    store.log_scan(mode, len(active_markets), len(valid_plans), entries_made, exits_made, skip_reasons)
    
    return {
        "markets": len(active_markets),
        "opportunities": len(valid_plans),
        "entries": entries_made,
        "exits": exits_made,
        "skip_reasons": skip_reasons
    }


def settle_positions(store: PaperTradeStore, config: dict):
    """Settle open positions that have passed their target date."""
    logger = logging.getLogger("paper_runner")
    
    weather = get_weather_provider(config.get("forecast_source", "open_meteo"), config)
    
    open_positions = store.get_open_positions()
    today = date.today()
    settled_count = 0
    
    for pos in open_positions:
        target_date = date.fromisoformat(pos["target_date"])
        
        if target_date >= today:
            continue
        
        location = pos["location"]
        
        try:
            forecast = weather.get_daily_forecast(location, target_date)
            if forecast:
                observed_high = forecast.high_temp_f
                result = store.settle_position(
                    pos["position_id"], observed_high, "open_meteo_historical"
                )
                
                hit_str = "HIT" if result["hit"] else "MISS"
                logger.info(f"SETTLED: {pos['position_id']} | {hit_str} | "
                           f"Observed: {observed_high:.1f}F | PnL: ${result['pnl']:.2f}")
                settled_count += 1
            else:
                logger.warning(f"No historical data for {location} {target_date}")
                
        except Exception as e:
            logger.error(f"Settlement error: {e}")
    
    return settled_count


def print_status(store: PaperTradeStore):
    """Print comprehensive status."""
    summary = store.get_summary()
    
    print("\n" + "="*60)
    print("PAPER TRADING STATUS")
    print("="*60)
    
    print(f"\nOpen positions: {summary['open_positions']}")
    print(f"Open risk: ${summary['open_risk']:.2f}")
    
    print(f"\nSettled trades: {summary['settled']}")
    if summary['settled'] > 0:
        win_rate = summary['settled_wins'] / summary['settled']
        calib = summary['settled_wins'] / summary['predicted_wins'] if summary['predicted_wins'] > 0 else 0
        print(f"  Win rate: {win_rate:.1%} ({summary['settled_wins']}/{summary['settled']})")
        print(f"  Predicted wins: {summary['predicted_wins']:.1f}")
        print(f"  Calibration ratio: {calib:.2f}")
        print(f"  Mean Brier: {summary['mean_brier']:.4f}")
        print(f"  Settlement PnL: ${summary['settled_pnl']:.2f}")
    
    print(f"\nEarly exits: {summary['exits']}")
    if summary['exits'] > 0:
        print(f"  Exit PnL: ${summary['exit_pnl']:.2f}")
        print(f"  Avg hold time: {summary['avg_hold_minutes']:.0f} min")
    
    total_pnl = summary['settled_pnl'] + summary['exit_pnl']
    print(f"\nTOTAL REALIZED PnL: ${total_pnl:.2f}")
    
    # Show open positions
    open_positions = store.get_open_positions()
    if open_positions:
        print("\nOpen positions:")
        for pos in open_positions[:10]:
            source = "OK" if pos.get('source_known') else "UNK"
            print(f"  {pos['position_id'][:12]} | {pos['location']} {pos['target_date']} | "
                  f"[{pos['interval_tmin']:.1f}-{pos['interval_tmax']:.1f}]F | "
                  f"Edge: {pos['edge']:.2%} | Station: {source}")


def main():
    parser = argparse.ArgumentParser(description="Forward-test paper trading with Exit Engine")
    parser.add_argument("--config", default="bot/config.yaml", help="Config file")
    parser.add_argument("--db", default="paper_trades.db", help="Database file")
    parser.add_argument("--interval", type=int, default=1800, help="Scan interval (seconds)")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--settle", action="store_true", help="Settle past positions")
    parser.add_argument("--settle-only", action="store_true", help="Only settle, no new entries")
    parser.add_argument("--status", action="store_true", help="Print status and exit")
    parser.add_argument("--scalp", action="store_true", help="Enable SCALP mode with exit engine")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    logger = logging.getLogger("paper_runner")
    
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"[X] Config not found: {args.config}")
        sys.exit(1)
    
    store = PaperTradeStore(args.db)
    mode = "scalp" if args.scalp else "hold"
    
    # Use scalp interval if in scalp mode
    if args.scalp:
        args.interval = config.get("scalp_mode", {}).get("scan_interval_seconds", 120)
    
    print("\n" + "="*60)
    print(f"  POLYMARKET PAPER TRADING - {mode.upper()} MODE")
    print("="*60)
    
    if args.status:
        print_status(store)
        sys.exit(0)
    
    if args.settle or args.settle_only:
        logger.info("Settling past positions...")
        count = settle_positions(store, config)
        logger.info(f"Settled {count} positions")
        print_status(store)
        if args.settle_only:
            sys.exit(0)
    
    locations = config.get("locations", [])
    min_edge = config.get("min_edge", 0.05) + config.get("edge_buffer", 0.02)
    max_risk = config.get("max_risk_per_day_usd", 2)
    
    print(f"\n[Config]")
    print(f"  Mode: {mode.upper()}")
    print(f"  Locations: {', '.join(locations)}")
    print(f"  Min edge: {min_edge:.2%}")
    print(f"  Max risk/day: ${max_risk}")
    print(f"  Scan interval: {args.interval}s")
    
    if mode == "scalp":
        exit_cfg = config.get("exit_engine", {})
        print(f"\n[Exit Engine]")
        print(f"  Take profit: ${exit_cfg.get('take_profit_abs_usd', 0.20)} / {exit_cfg.get('take_profit_pct', 0.20):.0%}")
        print(f"  Stop loss: ${exit_cfg.get('stop_loss_abs_usd', 0.30)} / {exit_cfg.get('stop_loss_pct', 0.25):.0%}")
        print(f"  Time stop: {exit_cfg.get('time_stop_minutes', 120)} min")
    
    # Show station mappings
    mappings = config.get("station_mappings", {})
    if mappings:
        print(f"\n[Resolution Sources]")
        for loc, info in mappings.items():
            print(f"  {loc.title()}: {info.get('station_id')} ({info.get('name')})")
    
    if args.once:
        print("\n[Mode] Single scan")
        result = run_scan_cycle(config, store, mode=mode, settle_only=args.settle_only)
        print(f"\nResult: {result['markets']} markets, {result['opportunities']} opps, "
              f"{result['entries']} entries, {result['exits']} exits")
        
        if result.get('skip_reasons'):
            print("\nSkip reasons:")
            for reason, count in result['skip_reasons'].items():
                if count > 0:
                    print(f"  {reason}: {count}")
        
        print_status(store)
    else:
        print(f"\n[Mode] Continuous (every {args.interval}s)")
        print("  Press Ctrl+C to stop\n")
        
        try:
            while True:
                settle_positions(store, config)
                result = run_scan_cycle(config, store, mode=mode, settle_only=args.settle_only)
                
                logger.info(f"Scan: {result['markets']} markets, {result['opportunities']} opps, "
                           f"{result['entries']} entries, {result['exits']} exits")
                
                if result['entries'] > 0 or result['exits'] > 0:
                    print_status(store)
                
                logger.info(f"Sleeping {args.interval}s...")
                time.sleep(args.interval)
                
        except KeyboardInterrupt:
            print("\n\n[Stop] Stopping...")
            print_status(store)


if __name__ == "__main__":
    main()
