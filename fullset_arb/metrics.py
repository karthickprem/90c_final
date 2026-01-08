"""
Metrics Module - Logging and Analytics

Handles:
- JSONL logging of all events
- Console reporting
- Daily summary generation
- Performance analytics
"""

import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, date
from pathlib import Path

from .config import ArbConfig, load_config
from .scanner import ArbOpportunity
from .executor import ExecutionResult, ExecutionStatus
from .ledger import Ledger

logger = logging.getLogger(__name__)


class MetricsLogger:
    """
    Logs all bot activity to JSONL files for analysis.
    
    File format: One JSON object per line, with timestamp and event type.
    """
    
    def __init__(self, config: ArbConfig = None):
        self.config = config or load_config()
        self.metrics_file = Path(self.config.metrics_file)
        
        # Ensure directory exists
        self.metrics_file.parent.mkdir(parents=True, exist_ok=True)
    
    def _write_event(self, event_type: str, data: Dict):
        """Write an event to the JSONL file."""
        event = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            **data
        }
        
        with open(self.metrics_file, "a") as f:
            f.write(json.dumps(event) + "\n")
    
    def log_opportunity(self, opp: ArbOpportunity):
        """Log a detected opportunity."""
        self._write_event("opportunity", opp.to_dict())
    
    def log_execution(self, result: ExecutionResult):
        """Log an execution result."""
        self._write_event("execution", result.to_dict())
    
    def log_scan_cycle(self, markets_scanned: int, opportunities_found: int, 
                       actionable_found: int, duration_ms: float):
        """Log a complete scan cycle."""
        self._write_event("scan_cycle", {
            "markets_scanned": markets_scanned,
            "opportunities_found": opportunities_found,
            "actionable_found": actionable_found,
            "duration_ms": duration_ms,
        })
    
    def log_daily_summary(self, summary: Dict):
        """Log daily summary."""
        self._write_event("daily_summary", summary)
    
    def log_error(self, error: str, context: Dict = None):
        """Log an error."""
        self._write_event("error", {
            "error": error,
            "context": context or {},
        })


class ConsoleReporter:
    """
    Prints formatted reports to console.
    """
    
    def __init__(self, ledger: Ledger = None):
        self.ledger = ledger
    
    def print_scan_summary(self, markets_scanned: int, opportunities: List[ArbOpportunity],
                           duration_ms: float):
        """Print summary after a scan cycle."""
        actionable = [o for o in opportunities if o.is_actionable]
        positive_edge_l1 = [o for o in opportunities if o.edge_l1 > 0]
        positive_edge_exec = [o for o in opportunities if o.edge_exec > 0]
        
        print(f"\n{'='*60}")
        print(f"SCAN COMPLETE | {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*60}")
        print(f"Markets scanned:       {markets_scanned}")
        print(f"Positive edge_l1:      {len(positive_edge_l1)}")
        print(f"Positive edge_exec:    {len(positive_edge_exec)}")
        print(f"Actionable:            {len(actionable)}")
        print(f"Duration:              {duration_ms:.0f}ms")
    
    def print_execution_summary(self, result: ExecutionResult):
        """Print summary of an execution."""
        # Use ASCII-safe symbols for Windows compatibility
        status_symbol = {
            ExecutionStatus.SUCCESS: "[OK]",
            ExecutionStatus.ONE_LEG_UNWOUND: "[WARN]",
            ExecutionStatus.BOTH_FAILED: "[FAIL]",
            ExecutionStatus.SKIPPED: "[SKIP]",
        }
        
        emoji = status_symbol.get(result.status, "[?]")
        
        print(f"\n{emoji} EXECUTION: {result.status.value}")
        print(f"   Market: {result.opportunity.market.slug[:50]}")
        print(f"   Edge: L1={result.opportunity.edge_l1:.4f} EXEC={result.opportunity.edge_exec:.4f}")
        
        if result.status == ExecutionStatus.SUCCESS:
            print(f"   Shares: {result.shares_filled:.2f}")
            print(f"   Cost: ${result.total_cost:.4f}")
            print(f"   Redemption: ${result.redemption_value:.4f}")
            print(f"   Realized P&L: ${result.realized_pnl:.4f}")
        
        elif result.status == ExecutionStatus.ONE_LEG_UNWOUND:
            print(f"   Unwind VWAP: ${result.unwind.unwind_vwap:.4f}")
            print(f"   Unwind Loss: ${result.unwind.unwind_loss:.4f} ({result.unwind.unwind_loss_pct:.2%})")
            print(f"   Realized P&L: ${result.realized_pnl:.4f}")
    
    def print_daily_summary(self):
        """Print daily summary from ledger."""
        if not self.ledger:
            return
        
        summaries = self.ledger.get_daily_summaries(days=1)
        if not summaries:
            print("\nNo data for today yet.")
            return
        
        s = summaries[0]
        
        print(f"\n{'='*60}")
        print(f"DAILY SUMMARY | {s['date']}")
        print(f"{'='*60}")
        print(f"Opportunities detected: {s['opportunities_total']}")
        print(f"  - Actionable:         {s['opportunities_actionable']}")
        print(f"Executions:             {s['executions_total']}")
        print(f"  - Success:            {s['executions_success']}")
        print(f"  - One-leg (unwound):  {s['executions_one_leg']}")
        print(f"  - Failed:             {s['executions_failed']}")
        print(f"Fill rate:              {s['avg_fill_rate']:.1f}%")
        print(f"Best edge:              {s['best_edge']:.4f}" if s['best_edge'] else "Best edge: N/A")
        print(f"Avg edge:               {s['avg_edge']:.4f}" if s['avg_edge'] else "Avg edge: N/A")
        print(f"Total P&L:              ${s['total_pnl']:.4f}")
        print(f"Unwind losses:          ${s['total_unwind_loss']:.4f}" if s['total_unwind_loss'] else "")
    
    def print_overall_stats(self):
        """Print overall statistics."""
        if not self.ledger:
            return
        
        stats = self.ledger.get_stats_summary()
        
        print(f"\n{'='*60}")
        print("OVERALL STATISTICS")
        print(f"{'='*60}")
        print(f"Total opportunities:    {stats['total_opportunities']}")
        print(f"  - Actionable:         {stats['actionable_opportunities']}")
        print(f"Total executions:       {stats['total_executions']}")
        print(f"  - Successful:         {stats['successful_executions']}")
        print(f"  - One-leg:            {stats['one_leg_executions']}")
        print(f"Success rate:           {stats['success_rate']:.1f}%")
        print(f"Avg edge:               {stats['avg_edge']:.4f}")
        print(f"Best edge:              {stats['best_edge']:.4f}")
        print(f"Total P&L:              ${stats['total_pnl']:.4f}")
        print(f"Total unwind loss:      ${stats['total_unwind_loss']:.4f}")
        print(f"Best trade:             ${stats['best_trade']:.4f}")
        print(f"Worst trade:            ${stats['worst_trade']:.4f}")
        print(f"Max drawdown:           ${stats['max_drawdown']:.4f}")
    
    def print_pnl_curve(self, days: int = 7):
        """Print P&L curve for past N days."""
        if not self.ledger:
            return
        
        curve = self.ledger.get_pnl_curve(days)
        
        if not curve:
            print("\nNo P&L data yet.")
            return
        
        print(f"\n{'='*60}")
        print(f"P&L CURVE (Last {days} days)")
        print(f"{'='*60}")
        print(f"{'Date':<12} {'Daily P&L':>12} {'Cumulative':>12}")
        print("-" * 40)
        
        for point in curve:
            daily = point['daily_pnl']
            cumulative = point['cumulative_pnl']
            daily_str = f"${daily:+.4f}" if daily else "$0.0000"
            cum_str = f"${cumulative:+.4f}"
            print(f"{point['date']:<12} {daily_str:>12} {cum_str:>12}")


class PerformanceAnalyzer:
    """
    Analyzes bot performance and provides insights.
    """
    
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
    
    def analyze_opportunity_frequency(self, days: int = 7) -> Dict:
        """Analyze how often opportunities appear."""
        summaries = self.ledger.get_daily_summaries(days)
        
        if not summaries:
            return {}
        
        total_opps = sum(s.get('opportunities_total', 0) or 0 for s in summaries)
        total_actionable = sum(s.get('opportunities_actionable', 0) or 0 for s in summaries)
        
        return {
            "days_analyzed": len(summaries),
            "total_opportunities": total_opps,
            "total_actionable": total_actionable,
            "avg_opportunities_per_day": total_opps / len(summaries),
            "avg_actionable_per_day": total_actionable / len(summaries),
            "actionable_rate": (total_actionable / total_opps * 100) if total_opps > 0 else 0,
        }
    
    def analyze_fill_quality(self, days: int = 7) -> Dict:
        """Analyze fill rate and one-leg risk."""
        summaries = self.ledger.get_daily_summaries(days)
        
        if not summaries:
            return {}
        
        total_exec = sum(s.get('executions_total', 0) or 0 for s in summaries)
        total_success = sum(s.get('executions_success', 0) or 0 for s in summaries)
        total_one_leg = sum(s.get('executions_one_leg', 0) or 0 for s in summaries)
        
        return {
            "total_executions": total_exec,
            "successful_fills": total_success,
            "one_leg_events": total_one_leg,
            "fill_rate": (total_success / total_exec * 100) if total_exec > 0 else 0,
            "one_leg_rate": (total_one_leg / total_exec * 100) if total_exec > 0 else 0,
        }
    
    def calculate_sharpe_ratio(self, days: int = 30) -> Optional[float]:
        """Calculate Sharpe ratio of daily returns."""
        curve = self.ledger.get_pnl_curve(days)
        
        if len(curve) < 2:
            return None
        
        daily_returns = [p['daily_pnl'] for p in curve]
        
        import statistics
        
        if len(daily_returns) < 2:
            return None
        
        mean_return = statistics.mean(daily_returns)
        std_return = statistics.stdev(daily_returns)
        
        if std_return == 0:
            return None
        
        # Annualized Sharpe (assuming 365 trading days)
        sharpe = (mean_return / std_return) * (365 ** 0.5)
        
        return sharpe
    
    def get_recommendations(self) -> List[str]:
        """Generate recommendations based on performance."""
        recommendations = []
        
        stats = self.ledger.get_stats_summary()
        
        # Check one-leg rate
        total_exec = stats.get('total_executions', 0)
        one_leg = stats.get('one_leg_executions', 0)
        if total_exec > 0:
            one_leg_rate = one_leg / total_exec
            if one_leg_rate > 0.1:  # More than 10% one-leg
                recommendations.append(
                    f"High one-leg rate ({one_leg_rate:.1%}). Consider tightening depth requirements."
                )
        
        # Check P&L
        total_pnl = stats.get('total_pnl', 0)
        unwind_loss = stats.get('total_unwind_loss', 0)
        
        if unwind_loss > abs(total_pnl) * 0.5:
            recommendations.append(
                "Unwind losses are significant. Consider increasing min_edge threshold."
            )
        
        # Check opportunity frequency
        freq = self.analyze_opportunity_frequency()
        if freq.get('avg_actionable_per_day', 0) < 1:
            recommendations.append(
                "Low opportunity frequency. Consider loosening filters (with caution)."
            )
        
        if not recommendations:
            recommendations.append("Performance looks healthy! Keep monitoring.")
        
        return recommendations


def main():
    """Test metrics."""
    logging.basicConfig(level=logging.INFO)
    
    config = ArbConfig()
    config.db_path = "test_fullset_arb.db"
    config.metrics_file = "test_metrics.jsonl"
    
    ledger = Ledger(config)
    metrics = MetricsLogger(config)
    reporter = ConsoleReporter(ledger)
    
    print("Metrics system initialized")
    
    # Print available reports
    reporter.print_daily_summary()
    reporter.print_overall_stats()
    reporter.print_pnl_curve()


if __name__ == "__main__":
    main()

