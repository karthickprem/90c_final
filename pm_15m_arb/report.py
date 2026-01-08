"""
Report Generator - Markdown Reports for Edge Validation

Generates comprehensive reports with:
- Total windows traded
- Opportunities seen vs taken
- Theoretical edge vs realized PnL
- Average/median pair cost for executed trades
- Slippage distribution
- Legging count + net impact
- PnL histogram per window
- Worst/best window
- "Would this survive 2x fees?" stress test

This is where the "edge" is proven (or disproven).
"""

import logging
import statistics
from typing import Dict, List, Any, Optional
from datetime import datetime, date
from pathlib import Path

from .config import ArbConfig, load_config
from .ledger import Ledger

logger = logging.getLogger(__name__)


class ReportGenerator:
    """
    Generates Markdown reports for paper trading analysis.
    
    The goal is to answer: Is there a persistent net edge after
    realistic slippage/fees and legging?
    """
    
    def __init__(self, config: ArbConfig = None, ledger: Ledger = None):
        self.config = config or load_config()
        self.ledger = ledger or Ledger(self.config)
    
    def generate(self, output_dir: str = None, days: int = 30) -> str:
        """
        Generate a comprehensive Markdown report.
        
        Args:
            output_dir: Directory to save report (default: current dir)
            days: Days of data to include
        
        Returns:
            Path to generated report file
        """
        output_dir = Path(output_dir or ".")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = output_dir / f"pm_15m_arb_report_{ts}.md"
        
        # Gather data
        stats = self.ledger.get_stats_summary()
        windows = self.ledger.get_windows(days=days)
        pnl_dist = self.ledger.get_pnl_distribution(days=days)
        legging = self.ledger.get_legging_summary()
        
        # Generate report sections
        sections = []
        
        sections.append(self._header_section())
        sections.append(self._summary_section(stats))
        sections.append(self._opportunity_section(stats, windows))
        sections.append(self._edge_analysis_section(stats, windows))
        sections.append(self._slippage_section(stats, windows))
        sections.append(self._legging_section(legging, stats))
        sections.append(self._pnl_distribution_section(pnl_dist, stats))
        sections.append(self._stress_test_section(stats))
        sections.append(self._recommendations_section(stats, legging))
        sections.append(self._config_section())
        
        # Write report
        report_content = "\n\n".join(sections)
        
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_content)
        
        logger.info(f"Report generated: {report_path}")
        return str(report_path)
    
    def _header_section(self) -> str:
        """Generate report header."""
        return f"""# PM 15m Arb - Paper Trading Report

**Generated:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

**Strategy:** Variant A (Paired Full-Set Arb)

**Question:** Is there a persistent net edge after realistic slippage/fees and legging?

---"""
    
    def _summary_section(self, stats: Dict) -> str:
        """Generate summary section."""
        return f"""## Executive Summary

| Metric | Value |
|--------|-------|
| Windows Traded | {stats.get('windows_count', 0)} |
| Total Trades | {stats.get('total_trades', 0)} |
| Successful Pairs | {stats.get('successful_pairs', 0)} |
| Legging Events | {stats.get('legging_events', 0)} |
| **Total P&L** | **${stats.get('total_pnl', 0):.4f}** |
| Avg P&L/Window | ${stats.get('avg_pnl_per_window', 0):.4f} |
| Best Window | ${stats.get('best_window_pnl', 0):.4f} |
| Worst Window | ${stats.get('worst_window_pnl', 0):.4f} |

### Verdict

{"✅ **POSITIVE NET EDGE**" if stats.get('total_pnl', 0) > 0 else "❌ **NEGATIVE RESULT**"} - {"Consider proceeding to live with small size" if stats.get('total_pnl', 0) > 0 else "Do not proceed to live trading"}"""
    
    def _opportunity_section(self, stats: Dict, windows: List[Dict]) -> str:
        """Generate opportunities analysis section."""
        total_signals_seen = sum(w.get('signals_seen', 0) for w in windows)
        total_signals_taken = sum(w.get('signals_taken', 0) for w in windows)
        
        take_rate = (total_signals_taken / max(1, total_signals_seen)) * 100
        
        return f"""## Opportunity Analysis

| Metric | Value |
|--------|-------|
| Total Signals Detected | {total_signals_seen} |
| Signals Executed | {total_signals_taken} |
| Take Rate | {take_rate:.1f}% |
| Avg Signals/Window | {total_signals_seen / max(1, len(windows)):.1f} |

### Interpretation

- **Signals seen:** Tick where `pair_cost <= 1 - min_edge - buffers`
- **Signals taken:** Actually executed trades
- **Low take rate:** May indicate tight filters or fast-moving prices
- **High take rate:** Signals are persistent and executable"""
    
    def _edge_analysis_section(self, stats: Dict, windows: List[Dict]) -> str:
        """Generate edge analysis section."""
        theoretical = stats.get('avg_theoretical_edge', 0)
        realized = stats.get('avg_realized_edge', 0)
        capture = stats.get('edge_capture_ratio', 0)
        
        # Calculate edge decay
        edge_decay = (theoretical - realized) / max(0.001, theoretical) * 100 if theoretical > 0 else 0
        
        return f"""## Edge Analysis

| Metric | Value |
|--------|-------|
| Avg Theoretical Edge | {theoretical:.4f} ({theoretical*100:.2f}%) |
| Avg Realized Edge | {realized:.4f} ({realized*100:.2f}%) |
| Edge Capture Ratio | {capture:.1%} |
| Edge Decay | {edge_decay:.1f}% |

### What This Means

- **Theoretical edge:** Edge at signal detection (from orderbook snapshot)
- **Realized edge:** Actual profit per share pair executed
- **Edge capture ratio:** What fraction of theoretical edge we actually capture
- **Good capture:** > 50% suggests executable edge
- **Poor capture:** < 30% suggests slippage/legging eating the edge

### Edge Breakdown

```
Theoretical Edge:     {theoretical*100:+.2f}%
- Execution slippage: -{stats.get('avg_slippage', 0)*100:.2f}%
- Legging losses:     -{edge_decay:.2f}% (estimated)
= Realized Edge:      {realized*100:+.2f}%
```"""
    
    def _slippage_section(self, stats: Dict, windows: List[Dict]) -> str:
        """Generate slippage analysis section."""
        avg_slippage = stats.get('avg_slippage', 0)
        max_slippage = stats.get('max_slippage', 0)
        
        return f"""## Slippage Analysis

| Metric | Value |
|--------|-------|
| Avg Slippage | ${avg_slippage:.4f} ({avg_slippage*100:.2f}%) |
| Max Slippage | ${max_slippage:.4f} ({max_slippage*100:.2f}%) |

### Slippage Sources

1. **Book movement:** Price changed between signal and execution
2. **VWAP vs L1:** Fill price walks deeper into book
3. **Execution delay:** Time between legs

### Buffer Adequacy

- Config slippage buffer: {self.config.slippage_buffer_per_leg:.4f}/leg
- Observed avg slippage: {avg_slippage:.4f}
- **{"✅ Buffer adequate" if self.config.slippage_buffer_per_leg >= avg_slippage else "⚠️ Consider increasing buffer"}**"""
    
    def _legging_section(self, legging: Dict, stats: Dict) -> str:
        """Generate legging analysis section."""
        total_legging = stats.get('legging_events', 0)
        total_pairs = stats.get('successful_pairs', 0)
        legging_rate = total_legging / max(1, total_pairs + total_legging) * 100
        
        total_loss = legging.get('total_loss', 0)
        
        return f"""## Legging Analysis

| Metric | Value |
|--------|-------|
| Total Legging Events | {total_legging} |
| Legging Rate | {legging_rate:.1f}% |
| Total Legging Loss | ${total_loss:.4f} |
| Avg Loss/Event | ${total_loss / max(1, total_legging):.4f} |

### Legging Events Breakdown

| Type | Count | Total Loss |
|------|-------|------------|
""" + "\n".join([
    f"| {e.get('event_type', 'Unknown')} ({e.get('action', '')}) | {e.get('count', 0)} | ${e.get('total_loss', 0):.4f} |"
    for e in legging.get('events', [])
]) + f"""

### Legging Risk Assessment

- **Legging rate < 5%:** ✅ Acceptable risk
- **Legging rate 5-15%:** ⚠️ Moderate risk, monitor closely
- **Legging rate > 15%:** ❌ High risk, needs investigation

Current: **{legging_rate:.1f}%** - {"✅ Acceptable" if legging_rate < 5 else "⚠️ Monitor" if legging_rate < 15 else "❌ High Risk"}"""
    
    def _pnl_distribution_section(self, pnl_dist: List[float], stats: Dict) -> str:
        """Generate P&L distribution section."""
        if not pnl_dist:
            return """## P&L Distribution

*No data available*"""
        
        # Calculate statistics
        mean_pnl = statistics.mean(pnl_dist)
        median_pnl = statistics.median(pnl_dist)
        stdev_pnl = statistics.stdev(pnl_dist) if len(pnl_dist) > 1 else 0
        
        # Count by bucket
        very_negative = sum(1 for p in pnl_dist if p < -0.1)
        negative = sum(1 for p in pnl_dist if -0.1 <= p < 0)
        zero = sum(1 for p in pnl_dist if p == 0)
        positive = sum(1 for p in pnl_dist if 0 < p <= 0.1)
        very_positive = sum(1 for p in pnl_dist if p > 0.1)
        
        # Win rate
        win_rate = sum(1 for p in pnl_dist if p > 0) / max(1, len(pnl_dist)) * 100
        
        return f"""## P&L Distribution

### Statistics

| Metric | Value |
|--------|-------|
| Mean P&L | ${mean_pnl:.4f} |
| Median P&L | ${median_pnl:.4f} |
| Std Dev | ${stdev_pnl:.4f} |
| Win Rate | {win_rate:.1f}% |
| Total Windows | {len(pnl_dist)} |

### Distribution Histogram

| Range | Count | % |
|-------|-------|---|
| < -$0.10 | {very_negative} | {very_negative/max(1,len(pnl_dist))*100:.1f}% |
| -$0.10 to $0 | {negative} | {negative/max(1,len(pnl_dist))*100:.1f}% |
| $0 | {zero} | {zero/max(1,len(pnl_dist))*100:.1f}% |
| $0 to $0.10 | {positive} | {positive/max(1,len(pnl_dist))*100:.1f}% |
| > $0.10 | {very_positive} | {very_positive/max(1,len(pnl_dist))*100:.1f}% |

### Best and Worst Windows

- **Best:** ${stats.get('best_window_pnl', 0):.4f}
- **Worst:** ${stats.get('worst_window_pnl', 0):.4f}
- **Range:** ${stats.get('best_window_pnl', 0) - stats.get('worst_window_pnl', 0):.4f}"""
    
    def _stress_test_section(self, stats: Dict) -> str:
        """Generate stress test section."""
        # Simulate with higher fees
        base_pnl = stats.get('total_pnl', 0)
        windows = stats.get('windows_count', 1)
        trades = stats.get('total_trades', 0)
        
        # Assume each trade would have 0.5% fee per leg with 2x fees
        simulated_2x_fee_cost = trades * 2 * 0.005  # 0.5% per leg × 2 legs × trades
        pnl_with_2x_fees = base_pnl - simulated_2x_fee_cost
        
        # Simulate with 2x slippage
        avg_slippage = stats.get('avg_slippage', 0.002)
        simulated_2x_slippage_cost = trades * 2 * avg_slippage
        pnl_with_2x_slippage = base_pnl - simulated_2x_slippage_cost
        
        # Combined stress
        pnl_stressed = base_pnl - simulated_2x_fee_cost - simulated_2x_slippage_cost
        
        return f"""## Stress Testing

### "Would This Survive Higher Costs?"

| Scenario | Simulated P&L | Survives? |
|----------|---------------|-----------|
| Base Case | ${base_pnl:.4f} | {"✅" if base_pnl > 0 else "❌"} |
| +0.5% fees/leg | ${pnl_with_2x_fees:.4f} | {"✅" if pnl_with_2x_fees > 0 else "❌"} |
| 2x slippage | ${pnl_with_2x_slippage:.4f} | {"✅" if pnl_with_2x_slippage > 0 else "❌"} |
| Both combined | ${pnl_stressed:.4f} | {"✅" if pnl_stressed > 0 else "❌"} |

### Robustness Score

Based on stress testing:

- **Survives 0/4 scenarios:** ❌ Extremely fragile, do not trade
- **Survives 1-2/4 scenarios:** ⚠️ Marginal, proceed with extreme caution
- **Survives 3/4 scenarios:** ✅ Reasonably robust
- **Survives 4/4 scenarios:** ✅✅ Very robust

**Current:** {sum([base_pnl > 0, pnl_with_2x_fees > 0, pnl_with_2x_slippage > 0, pnl_stressed > 0])}/4 scenarios survived"""
    
    def _recommendations_section(self, stats: Dict, legging: Dict) -> str:
        """Generate recommendations section."""
        recommendations = []
        
        # Check P&L
        total_pnl = stats.get('total_pnl', 0)
        if total_pnl < 0:
            recommendations.append("- ❌ **Negative P&L:** Do not proceed to live. Review strategy parameters.")
        else:
            recommendations.append("- ✅ **Positive P&L:** Strategy shows promise.")
        
        # Check edge capture
        capture = stats.get('edge_capture_ratio', 0)
        if capture < 0.3:
            recommendations.append("- ⚠️ **Low edge capture:** Consider tightening filters or increasing buffers.")
        
        # Check legging
        legging_events = stats.get('legging_events', 0)
        pairs = stats.get('successful_pairs', 0)
        legging_rate = legging_events / max(1, pairs + legging_events)
        if legging_rate > 0.1:
            recommendations.append("- ⚠️ **High legging rate:** Consider reducing order size or improving execution speed.")
        
        # Check sample size
        windows = stats.get('windows_count', 0)
        if windows < 50:
            recommendations.append(f"- ⚠️ **Small sample ({windows} windows):** Need more data before conclusions.")
        
        # Check slippage
        avg_slippage = stats.get('avg_slippage', 0)
        if avg_slippage > self.config.slippage_buffer_per_leg:
            recommendations.append(f"- ⚠️ **Slippage exceeds buffer:** Increase slippage_buffer_per_leg to {avg_slippage*1.5:.4f}")
        
        if not recommendations:
            recommendations.append("- ✅ All metrics look healthy!")
        
        return f"""## Recommendations

{chr(10).join(recommendations)}

### Next Steps

1. **If P&L positive with good sample size:**
   - Consider proceeding to live with $5-20 clips
   - Monitor first 10 windows closely
   - Set hard daily loss limit

2. **If P&L negative or marginal:**
   - Collect more data (target 100+ windows)
   - Review slippage and legging patterns
   - Adjust min_edge or buffer parameters
   - Consider whether edge is real or artifact"""
    
    def _config_section(self) -> str:
        """Generate config documentation section."""
        return f"""## Configuration Used

| Parameter | Value |
|-----------|-------|
| min_edge | {self.config.min_edge:.4f} |
| slippage_buffer_per_leg | {self.config.slippage_buffer_per_leg:.4f} |
| total_buffer_per_pair | {self.config.total_buffer_per_pair:.4f} |
| min_depth_shares | {self.config.min_depth_shares} |
| max_notional_per_window | ${self.config.max_notional_per_window} |
| order_size_usd | ${self.config.order_size_usd} |
| target_profit | ${self.config.target_profit} |
| stop_before_end | {self.config.stop_add_seconds_before_end}s |
| max_leg_timeout_ms | {self.config.max_leg_timeout_ms}ms |
| max_leg_slippage | {self.config.max_leg_slippage:.2%} |
| overlay_b | {'ENABLED' if self.config.enable_overlay_b else 'DISABLED'} |

---

*Report generated by pm_15m_arb v1.0*"""


def generate_quick_summary(config: ArbConfig = None) -> str:
    """Generate a quick one-page summary for console output."""
    config = config or load_config()
    ledger = Ledger(config)
    stats = ledger.get_stats_summary()
    
    verdict = "✅ POSITIVE" if stats.get('total_pnl', 0) > 0 else "❌ NEGATIVE"
    
    return f"""
{'='*50}
PM 15m Arb - Quick Summary
{'='*50}

Windows:    {stats.get('windows_count', 0)}
Trades:     {stats.get('total_trades', 0)}
P&L:        ${stats.get('total_pnl', 0):.4f}
Best:       ${stats.get('best_window_pnl', 0):.4f}
Worst:      ${stats.get('worst_window_pnl', 0):.4f}

Edge Capture: {stats.get('edge_capture_ratio', 0):.1%}
Legging:      {stats.get('legging_events', 0)} events

Verdict:    {verdict}
{'='*50}
"""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("\n=== Report Generator ===\n")
    
    config = ArbConfig()
    generator = ReportGenerator(config)
    
    # Generate report if data exists
    try:
        report_path = generator.generate()
        print(f"Report saved to: {report_path}")
        
        # Also print quick summary
        print(generate_quick_summary(config))
    except Exception as e:
        print(f"Could not generate report: {e}")
        print("Run paper trading first to collect data.")

