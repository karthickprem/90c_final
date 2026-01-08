"""
PnL Reconstruction - Best-effort P&L computation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from datetime import datetime

from .enrich import Episode
from .classify import Classification


@dataclass
class PnLResult:
    """P&L result for an episode."""
    episode_id: str
    market_id: str
    
    # Realized PnL
    realized_pnl: Optional[float] = None
    realized_via: Optional[str] = None  # "MERGE", "REDEEM", etc
    
    # Unrealized (if no resolution)
    unrealized_pnl: Optional[float] = None
    mark_price: Optional[float] = None
    
    # Costs
    total_cost: float = 0.0
    total_proceeds: float = 0.0
    fees_est: float = 0.0
    
    # Status
    is_realized: bool = False
    
    # Details
    details: dict = field(default_factory=dict)


def compute_episode_pnl(
    episode: Episode,
    classification: Classification,
    fee_bps: float = 0.0,
) -> PnLResult:
    """
    Compute PnL for an episode.
    
    Args:
        episode: The episode
        classification: Classification result
        fee_bps: Fee in basis points (e.g., 50 = 0.5%)
    
    Returns:
        PnLResult
    """
    episode_id = f"{episode.market_id[:20]}_{episode.start_ts.isoformat()[:19]}" if episode.start_ts else "unknown"
    
    result = PnLResult(
        episode_id=episode_id,
        market_id=episode.market_id or "unknown",
    )
    
    # Total costs
    result.total_cost = episode.total_cash_spent
    result.total_proceeds = episode.total_cash_received
    
    # Fee estimate
    if fee_bps > 0:
        volume = result.total_cost + result.total_proceeds
        result.fees_est = volume * (fee_bps / 10000)
    
    # =========================================================================
    # Case 1: MERGE (full-set arb)
    # =========================================================================
    if episode.has_merge and episode.matched_shares > 0:
        # Profit = matched_shares * (1.0 - avg_cost_matched) - fees
        edge = 1.0 - episode.avg_cost_matched
        gross_profit = episode.matched_shares * edge
        
        result.realized_pnl = gross_profit - result.fees_est
        result.realized_via = "MERGE"
        result.is_realized = True
        
        result.details = {
            'matched_shares': episode.matched_shares,
            'avg_cost_matched': episode.avg_cost_matched,
            'edge': edge,
            'edge_cents': edge * 100,
            'gross_profit': gross_profit,
        }
    
    # =========================================================================
    # Case 2: REDEEM (directional, held to resolution)
    # =========================================================================
    elif episode.has_redeem:
        # Check activity for cash from redeem
        redeem_cash = 0.0
        for a in episode.actions:
            if a.kind == "REDEEM" and a.cash_delta:
                redeem_cash += a.cash_delta
        
        if redeem_cash > 0:
            result.realized_pnl = redeem_cash - result.total_cost - result.fees_est
            result.realized_via = "REDEEM"
            result.is_realized = True
        else:
            # Can't determine redeem value
            result.unrealized_pnl = None
            result.realized_via = "REDEEM_UNKNOWN"
        
        result.details = {
            'redeem_cash': redeem_cash,
            'net_up': episode.net_up,
            'net_down': episode.net_down,
        }
    
    # =========================================================================
    # Case 3: Unrealized (still holding)
    # =========================================================================
    else:
        # Mark to market at 0.5 (unknown)
        result.mark_price = 0.5
        
        # Estimated unrealized
        position_value = (episode.net_up + episode.net_down) * result.mark_price
        result.unrealized_pnl = position_value + result.total_proceeds - result.total_cost - result.fees_est
        result.is_realized = False
        
        result.details = {
            'net_up': episode.net_up,
            'net_down': episode.net_down,
            'mark_price': result.mark_price,
        }
    
    return result


def compute_all_pnl(
    classifications: List[Tuple[Episode, Classification]],
    fee_bps: float = 0.0,
) -> List[PnLResult]:
    """Compute PnL for all episodes."""
    results = []
    
    for episode, classification in classifications:
        pnl = compute_episode_pnl(episode, classification, fee_bps)
        results.append(pnl)
    
    return results


def compute_pnl_summary(pnl_results: List[PnLResult]) -> dict:
    """Compute aggregate PnL summary."""
    realized = [r for r in pnl_results if r.is_realized and r.realized_pnl is not None]
    unrealized = [r for r in pnl_results if not r.is_realized]
    
    total_realized = sum(r.realized_pnl for r in realized)
    total_fees = sum(r.fees_est for r in pnl_results)
    
    # By realization type
    merge_pnl = sum(r.realized_pnl for r in realized if r.realized_via == "MERGE")
    redeem_pnl = sum(r.realized_pnl for r in realized if r.realized_via == "REDEEM")
    
    # Edge distribution (for merge episodes)
    edges = []
    for r in realized:
        if r.realized_via == "MERGE" and 'edge' in r.details:
            edges.append(r.details['edge'])
    
    avg_edge = sum(edges) / len(edges) if edges else 0
    min_edge = min(edges) if edges else 0
    max_edge = max(edges) if edges else 0
    
    # Holding times
    holding_times = []
    for r in realized:
        if r.realized_via == "MERGE":
            # Get from episode if available
            pass  # Would need episode reference
    
    return {
        'total_realized': total_realized,
        'total_fees_est': total_fees,
        'realized_count': len(realized),
        'unrealized_count': len(unrealized),
        'merge_pnl': merge_pnl,
        'redeem_pnl': redeem_pnl,
        'merge_count': len([r for r in realized if r.realized_via == "MERGE"]),
        'redeem_count': len([r for r in realized if r.realized_via == "REDEEM"]),
        'avg_edge': avg_edge,
        'min_edge': min_edge,
        'max_edge': max_edge,
        'edge_cents_avg': avg_edge * 100,
    }


def is_smooth_curve_from_merge(pnl_summary: dict, label_dist: dict) -> Tuple[bool, str]:
    """
    Determine if smooth curve comes from MERGE/full-set arb.
    
    Returns:
        (yes/no, evidence string)
    """
    merge_pct = label_dist['percentages'].get('FULL_SET_ARB', 0)
    merge_pnl = pnl_summary.get('merge_pnl', 0)
    total_realized = pnl_summary.get('total_realized', 0)
    merge_count = pnl_summary.get('merge_count', 0)
    
    # Build evidence
    evidence = []
    
    if merge_pct >= 50:
        evidence.append(f"FULL_SET_ARB is dominant label ({merge_pct:.1f}%)")
    
    if total_realized > 0 and merge_pnl / total_realized >= 0.7:
        evidence.append(f"MERGE accounts for {merge_pnl/total_realized*100:.1f}% of realized PnL")
    
    if merge_count >= 10:
        evidence.append(f"High MERGE count ({merge_count})")
    
    avg_edge = pnl_summary.get('avg_edge', 0)
    if avg_edge > 0:
        evidence.append(f"Avg edge per matched share: {avg_edge*100:.2f}c")
    
    # Decision
    is_merge_smooth = (
        merge_pct >= 40 or 
        (total_realized > 0 and merge_pnl / total_realized >= 0.5) or
        merge_count >= 20
    )
    
    if is_merge_smooth:
        answer = "YES"
        summary = "Smooth curve appears to come primarily from MERGE/full-set arb."
    else:
        answer = "NO"
        summary = "Smooth curve does NOT appear to come primarily from MERGE/full-set arb."
    
    evidence_str = "\n".join(f"  - {e}" for e in evidence) if evidence else "  - No strong evidence"
    
    return (answer == "YES"), f"{summary}\n\nEvidence:\n{evidence_str}"


