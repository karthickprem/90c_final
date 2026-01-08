"""
Classification V2 - Strategy inference with maker/taker and full-set detection
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Tuple
from collections import Counter

from .normalize import TradeEvent, ActivityEvent
from .pairing import MarketWindow, FullSetPair, compute_pairing_stats
from .config import DecoderV2Config


@dataclass
class MakerTakerStats:
    """Maker vs Taker statistics."""
    total_trades: int = 0
    maker_trades: int = 0
    taker_trades: int = 0
    unknown_trades: int = 0
    
    maker_volume: float = 0.0
    taker_volume: float = 0.0
    
    # Fee/rebate estimates
    total_fees_paid: float = 0.0
    total_rebates_earned: float = 0.0
    net_fee_impact: float = 0.0
    
    @property
    def maker_pct(self) -> float:
        if self.total_trades == 0:
            return 0
        return self.maker_trades / self.total_trades * 100
    
    @property
    def taker_pct(self) -> float:
        if self.total_trades == 0:
            return 0
        return self.taker_trades / self.total_trades * 100
    
    @property
    def maker_volume_pct(self) -> float:
        total = self.maker_volume + self.taker_volume
        if total == 0:
            return 0
        return self.maker_volume / total * 100


def compute_maker_taker_stats(
    trades: List[TradeEvent],
    config: DecoderV2Config,
) -> MakerTakerStats:
    """Compute maker/taker statistics."""
    stats = MakerTakerStats()
    stats.total_trades = len(trades)
    
    for t in trades:
        notional = t.price * t.size
        
        if t.liquidity == "MAKER":
            stats.maker_trades += 1
            stats.maker_volume += notional
            stats.total_rebates_earned += notional * config.maker_rebate_rate
        elif t.liquidity == "TAKER":
            stats.taker_trades += 1
            stats.taker_volume += notional
            stats.total_fees_paid += notional * config.taker_fee_rate
        else:
            stats.unknown_trades += 1
    
    stats.net_fee_impact = stats.total_rebates_earned - stats.total_fees_paid
    
    return stats


@dataclass
class StrategyHypothesis:
    """A strategy hypothesis with evidence."""
    name: str
    confidence: float  # 0..1
    description: str
    evidence: List[str] = field(default_factory=list)
    metrics: Dict = field(default_factory=dict)


def classify_strategy(
    trades: List[TradeEvent],
    activity: List[ActivityEvent],
    windows: Dict[str, MarketWindow],
    pairs: List[FullSetPair],
    maker_taker: MakerTakerStats,
    config: DecoderV2Config,
) -> List[StrategyHypothesis]:
    """
    Generate ranked strategy hypotheses.
    
    Returns list of hypotheses sorted by confidence.
    """
    hypotheses = []
    
    pair_stats = compute_pairing_stats(pairs)
    
    # =========================================================================
    # Hypothesis 1: REBATE MARKET MAKER
    # =========================================================================
    mm_score = 0.0
    mm_evidence = []
    
    # High trade count
    if len(trades) >= 10000:
        mm_score += 0.2
        mm_evidence.append(f"Very high trade count: {len(trades):,}")
    
    # High maker ratio
    if maker_taker.maker_pct >= 50:
        mm_score += 0.3
        mm_evidence.append(f"Maker ratio: {maker_taker.maker_pct:.1f}%")
    
    # Positive net rebates
    if maker_taker.net_fee_impact > 0:
        mm_score += 0.2
        mm_evidence.append(f"Net rebate earnings: ${maker_taker.net_fee_impact:.2f}")
    
    # Both sides traded frequently
    yes_trades = len([t for t in trades if t.outcome == "YES"])
    no_trades = len([t for t in trades if t.outcome == "NO"])
    if yes_trades > 0 and no_trades > 0:
        balance = min(yes_trades, no_trades) / max(yes_trades, no_trades)
        if balance > 0.4:
            mm_score += 0.15
            mm_evidence.append(f"Balanced two-sided trading: {balance:.1%}")
    
    hypotheses.append(StrategyHypothesis(
        name="REBATE_MARKET_MAKER",
        confidence=min(1.0, mm_score),
        description="High-frequency two-sided quoting to earn maker rebates + spread",
        evidence=mm_evidence,
        metrics={
            'trade_count': len(trades),
            'maker_pct': maker_taker.maker_pct,
            'net_rebates': maker_taker.net_fee_impact,
        },
    ))
    
    # =========================================================================
    # Hypothesis 2: FULL_SET_HOLD_TO_SETTLEMENT
    # =========================================================================
    fs_score = 0.0
    fs_evidence = []
    
    # Detected pairs
    if pair_stats['total_pairs'] > 0:
        fs_score += 0.2
        fs_evidence.append(f"Detected {pair_stats['total_pairs']} full-set pairs")
    
    # Profitable pairs
    if pair_stats['profitable_pct'] > 50:
        fs_score += 0.2
        fs_evidence.append(f"{pair_stats['profitable_pct']:.1f}% of pairs are profitable")
    
    # Average edge
    if pair_stats['avg_pair_edge'] > 0.02:  # >2c edge
        fs_score += 0.2
        fs_evidence.append(f"Avg pair edge: {pair_stats['avg_pair_edge']*100:.2f}c")
    
    # Pairs with redeem (held to settlement)
    if pair_stats['pairs_with_redeem'] > 0:
        redeem_pct = pair_stats['pairs_with_redeem'] / max(1, pair_stats['total_pairs']) * 100
        if redeem_pct > 30:
            fs_score += 0.2
            fs_evidence.append(f"{redeem_pct:.1f}% of pairs held to settlement")
    
    # Check for windows with matched inventory
    matched_windows = [w for w in windows.values() if w.matched_size > 0]
    if matched_windows:
        avg_matched = sum(w.matched_size for w in matched_windows) / len(matched_windows)
        fs_score += 0.1
        fs_evidence.append(f"{len(matched_windows)} windows with matched inventory (avg {avg_matched:.1f} shares)")
    
    hypotheses.append(StrategyHypothesis(
        name="FULL_SET_HOLD_TO_SETTLEMENT",
        confidence=min(1.0, fs_score),
        description="Buy YES + NO when cost < 1, hold to settlement for guaranteed profit",
        evidence=fs_evidence,
        metrics={
            'pairs_detected': pair_stats['total_pairs'],
            'avg_edge': pair_stats['avg_pair_edge'],
            'total_edge': pair_stats['total_gross_edge'],
        },
    ))
    
    # =========================================================================
    # Hypothesis 3: DIRECTIONAL / ORACLE SNIPER
    # =========================================================================
    dir_score = 0.0
    dir_evidence = []
    
    # Check for unbalanced positions
    total_yes = sum(t.size for t in trades if t.outcome == "YES" and t.side == "BUY")
    total_no = sum(t.size for t in trades if t.outcome == "NO" and t.side == "BUY")
    
    if total_yes > 0 and total_no > 0:
        imbalance = abs(total_yes - total_no) / (total_yes + total_no)
        if imbalance > 0.3:
            dir_score += 0.2
            side = "YES" if total_yes > total_no else "NO"
            dir_evidence.append(f"Position imbalance: {imbalance:.1%} toward {side}")
    
    # High taker ratio (aggressive entries)
    if maker_taker.taker_pct >= 60:
        dir_score += 0.2
        dir_evidence.append(f"High taker ratio: {maker_taker.taker_pct:.1f}%")
    
    # Check for spike entries (high price buys)
    spike_buys = [t for t in trades if t.side == "BUY" and t.price >= 0.85]
    if len(spike_buys) > len(trades) * 0.1:
        dir_score += 0.2
        dir_evidence.append(f"Many spike entries (price >= 85c): {len(spike_buys)}")
    
    # REDEEMs without MERGEs suggests holding to settlement
    redeem_count = len([a for a in activity if a.kind == "REDEEM"])
    merge_count = len([a for a in activity if a.kind == "MERGE"])
    
    if redeem_count > 0 and merge_count == 0:
        dir_score += 0.1
        dir_evidence.append(f"Settlement via REDEEM only ({redeem_count} redeems, 0 merges)")
    
    hypotheses.append(StrategyHypothesis(
        name="DIRECTIONAL_ORACLE_SNIPER",
        confidence=min(1.0, dir_score),
        description="Directional bets when market lags underlying (BTC spot)",
        evidence=dir_evidence,
        metrics={
            'yes_volume': total_yes,
            'no_volume': total_no,
            'spike_entries': len(spike_buys),
        },
    ))
    
    # =========================================================================
    # Hypothesis 4: HYBRID (combination)
    # =========================================================================
    # If multiple strategies have moderate confidence
    top_scores = sorted([h.confidence for h in hypotheses], reverse=True)
    if len(top_scores) >= 2 and top_scores[0] < 0.7 and top_scores[1] > 0.3:
        hybrid_evidence = [
            "Multiple strategy signals detected",
            f"Top 2 confidences: {top_scores[0]:.2f}, {top_scores[1]:.2f}",
        ]
        
        hypotheses.append(StrategyHypothesis(
            name="HYBRID_STRATEGY",
            confidence=0.5,
            description="Combination of market making, full-set arb, and directional",
            evidence=hybrid_evidence,
        ))
    
    # Sort by confidence
    hypotheses.sort(key=lambda h: h.confidence, reverse=True)
    
    return hypotheses


def get_best_hypothesis(hypotheses: List[StrategyHypothesis]) -> StrategyHypothesis:
    """Get the highest-confidence hypothesis."""
    if not hypotheses:
        return StrategyHypothesis(
            name="UNKNOWN",
            confidence=0.0,
            description="Insufficient data to classify",
        )
    return hypotheses[0]


