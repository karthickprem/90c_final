"""
Normalization V2 - Extended Event model with maker/taker fields
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any, Optional, Literal
import re


EventKind = Literal["TRADE", "MERGE", "SPLIT", "REDEEM", "REWARD", "TRANSFER", "UNKNOWN"]
OutcomeType = Literal["YES", "NO", None]
SideType = Literal["BUY", "SELL", None]
LiquidityType = Literal["MAKER", "TAKER", "UNKNOWN"]


@dataclass
class TradeEvent:
    """
    Enhanced trade event with maker/taker inference.
    """
    ts: datetime
    trade_id: str = ""
    
    # Market identification
    market_id: str = ""           # conditionId
    market_slug: str = ""         # Human-readable slug
    token_id: str = ""            # Specific outcome token
    
    # Trade details
    outcome: OutcomeType = None   # YES/NO (mapped from UP/DOWN)
    side: SideType = None         # BUY/SELL
    price: float = 0.0            # 0.00..1.00
    size: float = 0.0             # shares
    
    # Liquidity inference
    liquidity: LiquidityType = "UNKNOWN"
    
    # Fee/rebate estimation
    fee_paid: float = 0.0         # Estimated taker fee
    rebate_earned: float = 0.0    # Estimated maker rebate
    
    # Raw fields for debugging
    meta: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def notional(self) -> float:
        """Cash value of trade."""
        return self.price * self.size
    
    @property
    def is_buy(self) -> bool:
        return self.side == "BUY"
    
    @property
    def is_sell(self) -> bool:
        return self.side == "SELL"


@dataclass
class ActivityEvent:
    """Activity event (REDEEM, MERGE, etc.)."""
    ts: datetime
    kind: EventKind
    market_id: str = ""
    token_id: str = ""
    outcome: OutcomeType = None
    size: float = 0.0
    cash_delta: float = 0.0
    tx: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


def parse_timestamp(val: Any) -> Optional[datetime]:
    """Parse various timestamp formats."""
    if val is None:
        return None
    
    try:
        if isinstance(val, datetime):
            return val
        
        if isinstance(val, (int, float)):
            if val > 1e12:
                val = val / 1000
            return datetime.fromtimestamp(val)
        
        ts_str = str(val).replace("Z", "").split("+")[0].split(".")[0]
        return datetime.fromisoformat(ts_str)
    except:
        return None


def parse_price(val: Any) -> float:
    """Parse price to 0..1 float."""
    if val is None:
        return 0.0
    
    try:
        p = float(val)
        if p > 1.0:
            p = p / 100.0
        return max(0.0, min(1.0, p))
    except:
        return 0.0


def parse_outcome(raw: Any) -> OutcomeType:
    """
    Parse outcome to YES/NO.
    
    Maps UP→YES, DOWN→NO for BTC markets.
    """
    if raw is None:
        return None
    
    raw_lower = str(raw).lower().strip()
    
    if raw_lower in ("yes", "up", "a"):
        return "YES"
    if raw_lower in ("no", "down", "b"):
        return "NO"
    
    return None


def parse_side(val: Any) -> SideType:
    """Parse side to BUY/SELL."""
    if val is None:
        return None
    
    val_lower = str(val).lower().strip()
    
    if val_lower in ("buy", "bid", "long"):
        return "BUY"
    if val_lower in ("sell", "ask", "short"):
        return "SELL"
    
    return None


def infer_liquidity(
    trade: TradeEvent,
    nearby_best_bid: Optional[float] = None,
    nearby_best_ask: Optional[float] = None,
    tolerance: float = 0.005,
) -> LiquidityType:
    """
    Infer if trade was MAKER or TAKER.
    
    Logic:
    - BUY at price >= ask => TAKER (crossed the spread)
    - BUY at price < ask - tolerance => MAKER (resting order filled)
    - SELL at price <= bid => TAKER
    - SELL at price > bid + tolerance => MAKER
    """
    if nearby_best_bid is None or nearby_best_ask is None:
        # Try to infer from trade metadata
        is_maker = trade.meta.get("isMaker") or trade.meta.get("is_maker") or trade.meta.get("maker")
        if is_maker is True:
            return "MAKER"
        if is_maker is False:
            return "TAKER"
        
        # Heuristic: very aggressive price => taker
        if trade.side == "BUY" and trade.price >= 0.95:
            return "TAKER"  # Likely crossed
        if trade.side == "SELL" and trade.price <= 0.05:
            return "TAKER"
        
        return "UNKNOWN"
    
    if trade.side == "BUY":
        if trade.price >= nearby_best_ask:
            return "TAKER"
        elif trade.price < nearby_best_ask - tolerance:
            return "MAKER"
    
    elif trade.side == "SELL":
        if trade.price <= nearby_best_bid:
            return "TAKER"
        elif trade.price > nearby_best_bid + tolerance:
            return "MAKER"
    
    return "UNKNOWN"


def normalize_trade(raw: Dict[str, Any]) -> TradeEvent:
    """Normalize a raw trade record."""
    ts = parse_timestamp(
        raw.get("timestamp") or 
        raw.get("createdAt") or 
        raw.get("matchedAt") or
        raw.get("executedAt")
    )
    
    if ts is None:
        ts = datetime.now()
    
    trade_id = str(raw.get("id") or raw.get("tradeId") or raw.get("trade_id") or "")
    
    market_id = str(
        raw.get("conditionId") or 
        raw.get("condition_id") or
        raw.get("marketId") or
        ""
    )
    
    market_slug = str(raw.get("slug") or raw.get("marketSlug") or raw.get("question") or "")
    
    token_id = str(raw.get("tokenId") or raw.get("token_id") or raw.get("assetId") or "")
    
    outcome = parse_outcome(raw.get("outcome") or raw.get("asset"))
    side = parse_side(raw.get("side") or raw.get("type"))
    
    # If side not clear, try to infer
    if side is None:
        maker_side = raw.get("makerSide") or raw.get("maker_side")
        taker_side = raw.get("takerSide") or raw.get("taker_side")
        if taker_side:
            side = parse_side(taker_side)
    
    price = parse_price(raw.get("price") or raw.get("avgPrice"))
    size = float(raw.get("size") or raw.get("amount") or raw.get("quantity") or 0)
    
    trade = TradeEvent(
        ts=ts,
        trade_id=trade_id,
        market_id=market_id,
        market_slug=market_slug,
        token_id=token_id,
        outcome=outcome,
        side=side,
        price=price,
        size=size,
        meta=raw,
    )
    
    # Infer liquidity from metadata
    trade.liquidity = infer_liquidity(trade)
    
    return trade


ACTIVITY_TYPE_MAP = {
    "merge": "MERGE",
    "split": "SPLIT",
    "redeem": "REDEEM",
    "redemption": "REDEEM",
    "claim": "REDEEM",
    "reward": "REWARD",
    "transfer": "TRANSFER",
}


def normalize_activity(raw: Dict[str, Any]) -> ActivityEvent:
    """Normalize a raw activity record."""
    ts = parse_timestamp(
        raw.get("timestamp") or 
        raw.get("createdAt") or 
        raw.get("blockTimestamp")
    )
    
    if ts is None:
        ts = datetime.now()
    
    activity_type = str(raw.get("type") or raw.get("action") or "").lower()
    kind = ACTIVITY_TYPE_MAP.get(activity_type, "UNKNOWN")
    
    market_id = str(raw.get("conditionId") or raw.get("marketId") or "")
    token_id = str(raw.get("tokenId") or raw.get("token_id") or "")
    outcome = parse_outcome(raw.get("outcome") or raw.get("asset"))
    
    size = float(raw.get("amount") or raw.get("value") or raw.get("size") or 0)
    cash = float(raw.get("cashDelta") or raw.get("cash_delta") or raw.get("usdcAmount") or 0)
    
    tx = str(raw.get("transactionHash") or raw.get("tx") or "")
    
    return ActivityEvent(
        ts=ts,
        kind=kind,
        market_id=market_id,
        token_id=token_id,
        outcome=outcome,
        size=size,
        cash_delta=cash,
        tx=tx,
        meta=raw,
    )


def normalize_all(
    raw_trades: List[Dict],
    raw_activity: List[Dict],
) -> tuple[List[TradeEvent], List[ActivityEvent]]:
    """Normalize all raw data."""
    trades = []
    activity = []
    
    for raw in raw_trades:
        try:
            trades.append(normalize_trade(raw))
        except Exception as e:
            pass  # Skip malformed
    
    for raw in raw_activity:
        try:
            activity.append(normalize_activity(raw))
        except Exception as e:
            pass
    
    trades.sort(key=lambda t: t.ts)
    activity.sort(key=lambda a: a.ts)
    
    return trades, activity


