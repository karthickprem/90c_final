"""Metrics and analysis for simulation results."""
from dataclasses import dataclass
from typing import Dict, List, Tuple
import math

from .sim import SimulationResult


@dataclass
class HistogramDistance:
    """Distance metrics between two histograms."""
    l1_distance: float  # Sum of absolute differences (normalized)
    l2_distance: float  # Euclidean distance (normalized)
    kl_divergence: float  # KL divergence (if applicable)
    overlap_pct: float  # Percentage overlap


def normalize_histogram(hist: Dict[int, int]) -> Dict[int, float]:
    """Normalize histogram to sum to 1.0."""
    total = sum(hist.values())
    if total == 0:
        return {}
    return {k: v / total for k, v in hist.items()}


def compute_histogram_distance(
    actual: Dict[int, int],
    target: Dict[int, int]
) -> HistogramDistance:
    """Compute distance between two histograms."""
    # Normalize both
    actual_norm = normalize_histogram(actual)
    target_norm = normalize_histogram(target)
    
    # Get all keys
    all_keys = set(actual_norm.keys()) | set(target_norm.keys())
    
    # L1 distance
    l1 = 0.0
    for k in all_keys:
        l1 += abs(actual_norm.get(k, 0) - target_norm.get(k, 0))
    
    # L2 distance
    l2 = 0.0
    for k in all_keys:
        diff = actual_norm.get(k, 0) - target_norm.get(k, 0)
        l2 += diff * diff
    l2 = math.sqrt(l2)
    
    # KL divergence (target -> actual, with smoothing)
    epsilon = 1e-10
    kl = 0.0
    for k in target_norm:
        p = target_norm.get(k, epsilon)
        q = actual_norm.get(k, epsilon)
        if p > 0 and q > 0:
            kl += p * math.log(p / q)
    
    # Overlap percentage
    overlap = 0.0
    for k in all_keys:
        overlap += min(actual_norm.get(k, 0), target_norm.get(k, 0))
    
    return HistogramDistance(
        l1_distance=l1,
        l2_distance=l2,
        kl_divergence=kl,
        overlap_pct=overlap * 100
    )


def compute_summary_metrics(result: SimulationResult) -> Dict:
    """Compute comprehensive summary metrics from a simulation result."""
    metrics = {}
    
    # Basic counts
    metrics['windows_processed'] = result.windows_processed
    metrics['windows_with_activity'] = result.windows_with_activity
    metrics['activity_rate'] = result.windows_with_activity / max(1, result.windows_processed)
    
    metrics['total_pairs'] = result.total_pairs
    metrics['profitable_pairs'] = result.profitable_pairs
    metrics['profit_rate'] = result.profitable_pairs / max(1, result.total_pairs)
    
    metrics['chase_completed'] = result.chase_completed_pairs
    metrics['chase_rate'] = result.chase_completed_pairs / max(1, result.total_pairs)
    
    metrics['total_unwinds'] = result.total_unwinds
    metrics['unwind_rate'] = result.total_unwinds / max(1, result.windows_with_activity)
    
    # PnL metrics
    metrics['gross_edge_cents'] = result.gross_edge_cents
    metrics['unwind_loss_cents'] = result.unwind_loss_cents
    metrics['net_pnl_cents'] = result.net_pnl_cents
    
    metrics['gross_edge_dollars'] = result.gross_edge_cents / 100.0
    metrics['net_pnl_dollars'] = result.net_pnl_cents / 100.0
    
    # Per-pair metrics
    if result.total_pairs > 0:
        metrics['avg_pair_cost'] = sum(p.pair_cost for p in result.all_pairs) / result.total_pairs
        metrics['avg_edge_cents'] = result.gross_edge_cents / result.total_pairs
        metrics['avg_dt_secs'] = sum(p.dt_between_legs for p in result.all_pairs) / result.total_pairs
    else:
        metrics['avg_pair_cost'] = 0
        metrics['avg_edge_cents'] = 0
        metrics['avg_dt_secs'] = 0
    
    # Per-unwind metrics
    if result.total_unwinds > 0:
        metrics['avg_unwind_loss'] = result.unwind_loss_cents / result.total_unwinds
    else:
        metrics['avg_unwind_loss'] = 0
    
    # Strategy parameters
    metrics['d_cents'] = result.config.d_cents
    metrics['chase_timeout_secs'] = result.config.chase_timeout_secs
    metrics['max_pair_cost_cents'] = result.config.max_pair_cost_cents
    metrics['fill_model'] = result.config.fill_model
    
    return metrics


def rank_results_by_histogram_match(
    results: List[SimulationResult],
    target_hist: Dict[int, int]
) -> List[Tuple[SimulationResult, HistogramDistance]]:
    """Rank results by how well their pair_cost histogram matches target."""
    scored = []
    
    for result in results:
        dist = compute_histogram_distance(result.pair_cost_hist, target_hist)
        scored.append((result, dist))
    
    # Sort by L1 distance (lower is better)
    scored.sort(key=lambda x: x[1].l1_distance)
    
    return scored


def find_best_calibration(
    results: List[SimulationResult],
    target_hist: Dict[int, int],
    min_pairs: int = 100
) -> Tuple[SimulationResult, HistogramDistance]:
    """Find the best calibration that matches target histogram.
    
    Filters to results with at least min_pairs pairs.
    """
    # Filter by minimum pairs
    filtered = [r for r in results if r.total_pairs >= min_pairs]
    
    if not filtered:
        # Fall back to all results
        filtered = results
    
    ranked = rank_results_by_histogram_match(filtered, target_hist)
    
    if ranked:
        return ranked[0]
    return None, None


