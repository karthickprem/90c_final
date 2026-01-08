#!/usr/bin/env python3
"""
run_discover_debug.py - Ground-truth market discovery verification

Queries Polymarket with broad search terms to find ALL temperature-related
markets and shows exactly why each is accepted or rejected.

Usage:
    python run_discover_debug.py --broad temperature --limit 200
    python run_discover_debug.py --broad "highest temperature" --limit 100
    python run_discover_debug.py --terms temperature,weather,degrees --limit 300
"""

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum

import requests
import yaml

# Add bot directory to path
sys.path.insert(0, str(Path(__file__).parent))


class RejectReason(Enum):
    """Reasons a market was rejected."""
    ACCEPTED = "accepted"
    NO_QUESTION = "no_question_field"
    NO_TOKEN_IDS = "no_clob_token_ids"
    ORDERBOOK_DISABLED = "enable_order_book_false"
    PATTERN_MISMATCH = "no_temp_pattern_match"
    NO_NUMERIC_RANGE = "no_numeric_range_in_question"
    PARSE_FAILED = "bucket_parse_failed"
    DATE_PARSE_FAILED = "date_parse_failed"
    LOCATION_UNKNOWN = "location_not_recognized"
    MARKET_CLOSED = "market_closed"
    OUTSIDE_DATE_HORIZON = "outside_date_horizon"
    INVALID_BUCKET_WIDTH = "invalid_bucket_width"


@dataclass
class MarketAnalysis:
    """Analysis of a single market."""
    id: str
    slug: str
    question: str
    active: bool
    closed: bool
    enable_order_book: bool
    has_token_ids: bool
    token_id_count: int
    reject_reason: RejectReason
    reject_detail: str
    matched_pattern: Optional[str] = None
    parsed_tmin: Optional[float] = None
    parsed_tmax: Optional[float] = None
    parsed_date: Optional[str] = None
    parsed_location: Optional[str] = None


def load_config(config_path: str = "bot/config.yaml") -> dict:
    """Load configuration from YAML file."""
    try:
        with open(config_path) as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        return {}


class BroadMarketScanner:
    """Scanner that searches broadly and shows rejection reasons."""
    
    GAMMA_URL = "https://gamma-api.polymarket.com"
    
    # Very broad temperature patterns
    TEMP_KEYWORDS = [
        "temperature", "temp", "degrees", "fahrenheit", "celsius",
        "high", "low", "max", "min", "weather", "hot", "cold"
    ]
    
    # Patterns that indicate a temperature bucket market
    BUCKET_PATTERNS = [
        r'(\d+)\s*[–\-−]\s*(\d+)\s*[°]?\s*[fFcC]',  # 63-64F
        r'between\s+(\d+)\s+and\s+(\d+)',            # between 63 and 64
        r'from\s+(\d+)\s+to\s+(\d+)',                # from 63 to 64
        r'(\d+)\s+to\s+(\d+)\s*[°]?\s*[fFcC]',       # 63 to 64F
    ]
    
    # Location patterns
    LOCATIONS = [
        "london", "new york", "nyc", "los angeles", "la", "chicago",
        "tokyo", "paris", "berlin", "sydney", "miami", "houston",
        "phoenix", "seattle", "denver", "boston", "atlanta", "dallas",
        "san francisco", "sf", "washington", "dc"
    ]
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PolymarketDiscoveryDebug/1.0",
            "Accept": "application/json"
        })
    
    def _get(self, endpoint: str, params: dict = None, timeout: float = 30.0) -> Any:
        """Make GET request to Gamma API."""
        url = f"{self.GAMMA_URL}{endpoint}"
        response = self.session.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()
    
    def search_markets_broad(self, search_terms: List[str], limit: int = 200,
                              include_closed: bool = True) -> List[dict]:
        """
        Search markets broadly using multiple terms and pagination.
        """
        all_markets = []
        seen_ids = set()
        
        # Try different endpoint approaches
        endpoints_to_try = [
            ("/markets", {"limit": 100, "closed": "false", "active": "true"}),
            ("/markets", {"limit": 100, "closed": "true", "active": "false"}),
        ]
        
        for endpoint, base_params in endpoints_to_try:
            if len(all_markets) >= limit:
                break
                
            offset = 0
            max_pages = (limit // 100) + 2
            
            for page in range(max_pages):
                if len(all_markets) >= limit:
                    break
                    
                params = {**base_params, "offset": offset}
                
                try:
                    markets = self._get(endpoint, params)
                    if not markets:
                        break
                    
                    for market in markets:
                        market_id = market.get("id", "")
                        if market_id and market_id not in seen_ids:
                            question = (market.get("question") or "").lower()
                            
                            # Check if any search term matches
                            matches_term = any(
                                term.lower() in question 
                                for term in search_terms
                            )
                            
                            if matches_term or not search_terms:
                                seen_ids.add(market_id)
                                all_markets.append(market)
                    
                    offset += 100
                    
                except Exception as e:
                    print(f"Error querying {endpoint}: {e}")
                    break
        
        return all_markets[:limit]
    
    def _check_temp_pattern(self, question: str) -> Tuple[bool, str]:
        """Check if question matches temperature patterns."""
        q_lower = question.lower()
        
        # Must have temperature-related keyword
        has_temp_keyword = any(kw in q_lower for kw in self.TEMP_KEYWORDS)
        
        if not has_temp_keyword:
            return False, "no_temp_keyword"
        
        return True, "has_temp_keyword"
    
    def _check_numeric_range(self, question: str) -> Tuple[bool, Optional[Tuple[float, float]], str]:
        """Check for numeric range pattern."""
        for pattern in self.BUCKET_PATTERNS:
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                try:
                    tmin = float(match.group(1))
                    tmax = float(match.group(2))
                    if tmin < tmax:
                        return True, (tmin, tmax), pattern
                except (ValueError, IndexError):
                    continue
        
        return False, None, "no_pattern_match"
    
    def _check_location(self, question: str) -> Tuple[bool, Optional[str]]:
        """Check for location in question."""
        q_lower = question.lower()
        for loc in self.LOCATIONS:
            if loc in q_lower:
                return True, loc
        return False, None
    
    def analyze_market(self, market: dict) -> MarketAnalysis:
        """Analyze a single market and determine accept/reject reason."""
        market_id = market.get("id", "unknown")
        slug = market.get("slug", "")
        question = market.get("question", "")
        active = market.get("active", False)
        closed = market.get("closed", False)
        enable_order_book = market.get("enableOrderBook", True)
        
        clob_ids = market.get("clobTokenIds") or market.get("clob_token_ids") or []
        has_token_ids = len(clob_ids) > 0
        
        # Start with ACCEPTED, then check for rejections
        reject_reason = RejectReason.ACCEPTED
        reject_detail = ""
        matched_pattern = None
        parsed_tmin = None
        parsed_tmax = None
        parsed_date = None
        parsed_location = None
        
        # Check 1: Has question?
        if not question:
            reject_reason = RejectReason.NO_QUESTION
            reject_detail = "Market has no question field"
        
        # Check 2: Has token IDs?
        elif not has_token_ids:
            reject_reason = RejectReason.NO_TOKEN_IDS
            reject_detail = f"No CLOB token IDs (clobTokenIds={clob_ids})"
        
        # Check 3: Orderbook enabled?
        elif not enable_order_book:
            reject_reason = RejectReason.ORDERBOOK_DISABLED
            reject_detail = "enableOrderBook=false"
        
        # Check 4: Temperature pattern?
        else:
            has_temp, temp_detail = self._check_temp_pattern(question)
            if not has_temp:
                reject_reason = RejectReason.PATTERN_MISMATCH
                reject_detail = f"No temperature keyword found"
            else:
                # Check 5: Numeric range?
                has_range, range_vals, pattern = self._check_numeric_range(question)
                if not has_range:
                    reject_reason = RejectReason.NO_NUMERIC_RANGE
                    reject_detail = "Has temp keyword but no numeric range (not a bucket market)"
                else:
                    matched_pattern = pattern
                    parsed_tmin, parsed_tmax = range_vals
                    
                    # Check 6: Valid bucket width?
                    width = parsed_tmax - parsed_tmin
                    if width < 0.5 or width > 10:
                        reject_reason = RejectReason.INVALID_BUCKET_WIDTH
                        reject_detail = f"Bucket width {width}F outside valid range [0.5, 10]"
                    else:
                        # Check 7: Location?
                        has_loc, loc = self._check_location(question)
                        if has_loc:
                            parsed_location = loc
                        # Don't reject for missing location, just note it
                        
                        # If we get here, it's a valid temperature bucket market
                        reject_reason = RejectReason.ACCEPTED
                        reject_detail = f"Valid bucket market: {parsed_tmin}-{parsed_tmax}F"
                        if parsed_location:
                            reject_detail += f" in {parsed_location}"
        
        return MarketAnalysis(
            id=market_id,
            slug=slug,
            question=question[:200],  # Truncate for display
            active=active,
            closed=closed,
            enable_order_book=enable_order_book,
            has_token_ids=has_token_ids,
            token_id_count=len(clob_ids),
            reject_reason=reject_reason,
            reject_detail=reject_detail,
            matched_pattern=matched_pattern,
            parsed_tmin=parsed_tmin,
            parsed_tmax=parsed_tmax,
            parsed_date=parsed_date,
            parsed_location=parsed_location
        )
    
    def scan_and_analyze(self, search_terms: List[str], limit: int = 200) -> List[MarketAnalysis]:
        """Scan markets and analyze each one."""
        print(f"\nSearching Gamma API for terms: {search_terms}")
        print(f"Limit: {limit} markets\n")
        
        markets = self.search_markets_broad(search_terms, limit)
        print(f"Found {len(markets)} markets matching search terms\n")
        
        analyses = []
        for market in markets:
            analysis = self.analyze_market(market)
            analyses.append(analysis)
        
        return analyses


def print_summary(analyses: List[MarketAnalysis]):
    """Print summary of analysis results."""
    print("\n" + "="*70)
    print("DISCOVERY ANALYSIS SUMMARY")
    print("="*70)
    
    total = len(analyses)
    accepted = [a for a in analyses if a.reject_reason == RejectReason.ACCEPTED]
    
    print(f"\nTotal markets analyzed: {total}")
    print(f"Accepted (valid temp bucket): {len(accepted)}")
    print(f"Rejected: {total - len(accepted)}")
    
    # Group by rejection reason
    by_reason: Dict[RejectReason, List[MarketAnalysis]] = {}
    for a in analyses:
        if a.reject_reason not in by_reason:
            by_reason[a.reject_reason] = []
        by_reason[a.reject_reason].append(a)
    
    print("\nBreakdown by reason:")
    for reason, items in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        print(f"  {reason.value}: {len(items)}")
    
    # Show accepted markets
    if accepted:
        print(f"\n{'='*70}")
        print("ACCEPTED TEMPERATURE BUCKET MARKETS")
        print("="*70)
        for a in accepted[:20]:
            status = "[ACTIVE]" if a.active else "[CLOSED]"
            print(f"\n{status} {a.slug}")
            print(f"  ID: {a.id}")
            print(f"  Question: {a.question}")
            print(f"  Parsed: {a.parsed_tmin}-{a.parsed_tmax}F")
            if a.parsed_location:
                print(f"  Location: {a.parsed_location}")
            print(f"  Orderbook: {a.enable_order_book}, Tokens: {a.token_id_count}")
    
    # Show sample rejections
    print(f"\n{'='*70}")
    print("SAMPLE REJECTED MARKETS (first 5 per reason)")
    print("="*70)
    
    for reason in [r for r in RejectReason if r != RejectReason.ACCEPTED]:
        rejected = by_reason.get(reason, [])
        if rejected:
            print(f"\n--- {reason.value} ({len(rejected)} total) ---")
            for a in rejected[:5]:
                print(f"  Q: {a.question[:80]}...")
                print(f"     -> {a.reject_detail}")


def save_to_jsonl(analyses: List[MarketAnalysis], filepath: str):
    """Save all analyses to JSONL file."""
    with open(filepath, "w") as f:
        for a in analyses:
            record = {
                "id": a.id,
                "slug": a.slug,
                "question": a.question,
                "active": a.active,
                "closed": a.closed,
                "enable_order_book": a.enable_order_book,
                "has_token_ids": a.has_token_ids,
                "token_id_count": a.token_id_count,
                "reject_reason": a.reject_reason.value,
                "reject_detail": a.reject_detail,
                "matched_pattern": a.matched_pattern,
                "parsed_tmin": a.parsed_tmin,
                "parsed_tmax": a.parsed_tmax,
                "parsed_location": a.parsed_location,
            }
            f.write(json.dumps(record) + "\n")
    print(f"\nSaved {len(analyses)} market analyses to {filepath}")


def main():
    parser = argparse.ArgumentParser(description="Debug market discovery")
    parser.add_argument("--broad", default="temperature", 
                       help="Broad search term (default: temperature)")
    parser.add_argument("--terms", default=None,
                       help="Comma-separated search terms")
    parser.add_argument("--limit", type=int, default=200,
                       help="Max markets to analyze (default: 200)")
    parser.add_argument("--output", default="discover_dump.jsonl",
                       help="Output JSONL file")
    parser.add_argument("--all", action="store_true",
                       help="Scan ALL markets (no term filter)")
    args = parser.parse_args()
    
    print("\n" + "="*70)
    print("  POLYMARKET MARKET DISCOVERY DEBUG")
    print("="*70)
    
    # Determine search terms
    if args.all:
        search_terms = []
        print("\nMode: Scanning ALL markets (no filter)")
    elif args.terms:
        search_terms = [t.strip() for t in args.terms.split(",")]
    else:
        search_terms = [args.broad]
    
    if search_terms:
        print(f"\nSearch terms: {search_terms}")
    print(f"Limit: {args.limit}")
    
    # Load config
    config = load_config()
    
    # Scan and analyze
    scanner = BroadMarketScanner(config)
    
    try:
        analyses = scanner.scan_and_analyze(search_terms, args.limit)
    except Exception as e:
        print(f"\n[X] API Error: {e}")
        print("\nThis could mean:")
        print("  - Network connectivity issue")
        print("  - Gamma API is down or blocking")
        print("  - Rate limiting")
        sys.exit(1)
    
    # Print summary
    print_summary(analyses)
    
    # Save to file
    save_to_jsonl(analyses, args.output)
    
    # Final verdict
    accepted = [a for a in analyses if a.reject_reason == RejectReason.ACCEPTED]
    active_accepted = [a for a in accepted if a.active]
    
    print("\n" + "="*70)
    print("VERDICT")
    print("="*70)
    
    if active_accepted:
        print(f"\n[OK] Found {len(active_accepted)} ACTIVE temperature bucket markets!")
        print("     Your main scanner should be finding these.")
        print("     If not, the issue is in the main scanner logic, not the API.")
    elif accepted:
        print(f"\n[!] Found {len(accepted)} temperature bucket markets, but ALL are CLOSED.")
        print("    No active temperature markets exist right now.")
        print("    Run paper trading to catch them when they appear.")
    else:
        print("\n[X] Found ZERO temperature bucket markets.")
        print("    Either:")
        print("    - Polymarket has no temperature bucket markets currently")
        print("    - The search terms are wrong")
        print("    - The bucket parsing patterns are too strict")
        print("\n    Try: python run_discover_debug.py --all --limit 500")
        print("    This scans all markets without term filtering.")


if __name__ == "__main__":
    main()





