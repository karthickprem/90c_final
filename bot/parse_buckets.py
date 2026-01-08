"""
Parser for temperature bucket market questions.
Extracts temperature range, date, and location from Polymarket question text.

Handles actual Polymarket formats:
- "Will the highest temperature in New York City be 27F or below on January 3?"
- "Will the highest temperature in New York City be between 28-29F on January 3?"
- "Will the highest temperature in Seoul be 0C on January 3?"
- "Will the highest temperature in Atlanta be 68F or higher on January 3?"
"""

import re
import logging
from typing import Optional, Tuple, List
from datetime import date, datetime
from dateutil import parser as date_parser

logger = logging.getLogger(__name__)

# Month name mappings
MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# Known locations with normalization
LOCATION_ALIASES = {
    "new york city": "New York",
    "nyc": "New York",
    "la": "Los Angeles",
    "sf": "San Francisco",
    "buenos aires": "Buenos Aires",
}

KNOWN_LOCATIONS = [
    "london", "new york city", "new york", "nyc", "los angeles", "chicago",
    "tokyo", "paris", "berlin", "sydney", "mumbai", "beijing", "seoul",
    "miami", "houston", "phoenix", "seattle", "denver", "boston",
    "atlanta", "dallas", "san francisco", "sf", "buenos aires",
]


def celsius_to_fahrenheit(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return c * 9 / 5 + 32


def fahrenheit_to_celsius(f: float) -> float:
    """Convert Fahrenheit to Celsius."""
    return (f - 32) * 5 / 9


def parse_temperature_question_v2(question: str) -> Optional[Tuple[float, float, date, Optional[str], str, bool, Optional[str]]]:
    """
    Parse a Polymarket temperature bucket question (v2 - handles actual formats).
    
    Returns tuple of:
        (tmin_f, tmax_f, target_date, location, temp_unit, is_tail_bucket, tail_type)
    
    Where:
        - tmin_f, tmax_f: Temperature range in Fahrenheit
        - target_date: The date for the temperature
        - location: City/location name
        - temp_unit: Original unit ("F" or "C")
        - is_tail_bucket: True if "X or below" / "X or higher"
        - tail_type: "lower" or "upper" for tail buckets, None otherwise
    
    Returns None if parsing fails.
    
    Supported formats:
        - "be 27F or below" -> lower tail [-inf, 28)
        - "be between 28-29F" -> bucket [28, 30)
        - "be 0C" (single degree) -> bucket [0C, 1C)
        - "be 68F or higher" -> upper tail [68, +inf)
    """
    q = question
    
    # Step 1: Extract location
    location = extract_location_v2(q)
    
    # Step 2: Extract date
    target_date = extract_date(q)
    if not target_date:
        logger.debug(f"No date found: {q[:60]}")
        return None
    
    # Step 3: Determine temperature unit
    # Check for C or F in the question
    has_celsius = bool(re.search(r'\d+\s*°?\s*[cC](?:\s|$|or|on)', q))
    has_fahrenheit = bool(re.search(r'\d+\s*°?\s*[fF](?:\s|$|or|on)', q))
    
    if has_celsius and not has_fahrenheit:
        temp_unit = "C"
    else:
        temp_unit = "F"  # Default to Fahrenheit
    
    # Step 4: Try to parse the temperature specification
    # Pattern order matters - try most specific first
    
    # Pattern A: "be between X-Y°F" or "be between X-Y°C"
    # CRITICAL: "between 28-29F" means [28, 30) if it's a 2°F bucket
    # The market says "between 28-29" which is INCLUSIVE of both endpoints
    # So the bucket is [28, 30) to catch temps in [28.0, 30.0)
    # Actually NO - if market says "28-29" it means the bucket covers 28-29,
    # which is [28, 30) only if we want exclusive upper bound.
    # Let's check: the tmax in the question IS the upper value, bucket is [tmin, tmax+1)
    range_pattern = r'be\s+between\s+(-?\d+(?:\.\d+)?)\s*[–\-−]\s*(-?\d+(?:\.\d+)?)\s*°?\s*([fFcC])?'
    match = re.search(range_pattern, q, re.IGNORECASE)
    if match:
        tmin = float(match.group(1))
        tmax = float(match.group(2))
        unit_match = match.group(3)
        if unit_match:
            temp_unit = unit_match.upper()
        
        # "between 28-29F" means temps in [28, 30) - i.e., 28.0 to 29.999...
        # The upper bound in the question (29) is INCLUSIVE
        # So our [tmin, tmax) representation needs tmax = question_tmax + 1
        bucket_width = tmax - tmin + 1  # e.g., 29 - 28 + 1 = 2
        
        if temp_unit == "C":
            tmin_f = celsius_to_fahrenheit(tmin)
            tmax_f = celsius_to_fahrenheit(tmin + bucket_width)
        else:
            tmin_f = tmin
            tmax_f = tmin + bucket_width  # [28, 30) for "28-29"
        
        return (tmin_f, tmax_f, target_date, location, temp_unit, False, None)
    
    # Pattern B: "be X°F or below" / "be X°C or below"
    below_pattern = r'be\s+(-?\d+(?:\.\d+)?)\s*°?\s*([fFcC])?\s+or\s+(?:below|lower|less)'
    match = re.search(below_pattern, q, re.IGNORECASE)
    if match:
        threshold = float(match.group(1))
        unit_match = match.group(2)
        if unit_match:
            temp_unit = unit_match.upper()
        
        # For "X or below", the bucket wins if temp <= X
        # We model this as [-inf, X+1) in our [tmin, tmax) convention
        if temp_unit == "C":
            # e.g., "-1C or below" means temp <= -1C, bucket is [-inf, 0C)
            tmax_f = celsius_to_fahrenheit(threshold + 1)
            tmin_f = -999  # Represents -infinity
        else:
            tmax_f = threshold + 1
            tmin_f = -999
        
        return (tmin_f, tmax_f, target_date, location, temp_unit, True, "lower")
    
    # Pattern C: "be X°F or higher" / "be X°C or higher" / "or above"
    above_pattern = r'be\s+(-?\d+(?:\.\d+)?)\s*°?\s*([fFcC])?\s+or\s+(?:higher|above|more|greater)'
    match = re.search(above_pattern, q, re.IGNORECASE)
    if match:
        threshold = float(match.group(1))
        unit_match = match.group(2)
        if unit_match:
            temp_unit = unit_match.upper()
        
        # For "X or higher", the bucket wins if temp >= X
        # We model this as [X, +inf) in our [tmin, tmax) convention
        if temp_unit == "C":
            tmin_f = celsius_to_fahrenheit(threshold)
            tmax_f = 999  # Represents +infinity
        else:
            tmin_f = threshold
            tmax_f = 999
        
        return (tmin_f, tmax_f, target_date, location, temp_unit, True, "upper")
    
    # Pattern D: "be X°C" or "be X°F" (single degree bucket)
    # This is common for Celsius markets: "be 0°C on January 3"
    single_pattern = r'be\s+(-?\d+(?:\.\d+)?)\s*°?\s*([fFcC])\s+on'
    match = re.search(single_pattern, q, re.IGNORECASE)
    if match:
        temp = float(match.group(1))
        unit_match = match.group(2)
        temp_unit = unit_match.upper()
        
        # Single degree bucket: [X, X+1)
        if temp_unit == "C":
            tmin_f = celsius_to_fahrenheit(temp)
            tmax_f = celsius_to_fahrenheit(temp + 1)
        else:
            tmin_f = temp
            tmax_f = temp + 1
        
        return (tmin_f, tmax_f, target_date, location, temp_unit, False, None)
    
    # Pattern E: Fallback - any range pattern "X-Y°F"
    fallback_range = r'(-?\d+(?:\.\d+)?)\s*[–\-−]\s*(-?\d+(?:\.\d+)?)\s*°?\s*([fFcC])?'
    match = re.search(fallback_range, q)
    if match:
        tmin = float(match.group(1))
        tmax = float(match.group(2))
        unit_match = match.group(3)
        if unit_match:
            temp_unit = unit_match.upper()
        
        if temp_unit == "C":
            tmin_f = celsius_to_fahrenheit(tmin)
            tmax_f = celsius_to_fahrenheit(tmax + 1)
        else:
            tmin_f = tmin
            tmax_f = tmax + 1
        
        return (tmin_f, tmax_f, target_date, location, temp_unit, False, None)
    
    logger.debug(f"No temperature pattern matched: {q[:60]}")
    return None


def extract_location_v2(question: str) -> Optional[str]:
    """
    Extract location from question text (v2 - handles actual formats).
    Returns normalized location name.
    """
    q_lower = question.lower()
    
    # Try to match "in <Location>" pattern
    in_pattern = r'(?:temperature|temp|high)\s+in\s+([A-Za-z][A-Za-z\s]+?)(?:\s+be\s|\s+on\s)'
    match = re.search(in_pattern, question, re.IGNORECASE)
    if match:
        loc = match.group(1).strip()
        # Normalize
        loc_lower = loc.lower()
        if loc_lower in LOCATION_ALIASES:
            return LOCATION_ALIASES[loc_lower]
        return loc.title()
    
    # Direct matching for known locations
    for loc in sorted(KNOWN_LOCATIONS, key=len, reverse=True):  # Longest first
        if loc in q_lower:
            if loc in LOCATION_ALIASES:
                return LOCATION_ALIASES[loc]
            return loc.title()
    
    return None


def extract_date(question: str, reference_year: int = None) -> Optional[date]:
    """
    Extract target date from question text.
    Uses current year or next occurrence if date has passed.
    """
    if reference_year is None:
        reference_year = datetime.now().year
    
    today = date.today()
    
    # Pattern: "on January 3" or "on Jan 3"
    date_patterns = [
        r'on\s+(january|february|march|april|may|june|july|august|september|october|november|december|'
        r'jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s+(\d{1,2})(?:st|nd|rd|th)?',
        r'(january|february|march|april|may|june|july|august|september|october|november|december|'
        r'jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s+(\d{1,2})(?:st|nd|rd|th)?',
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, question, re.IGNORECASE)
        if match:
            month_str = match.group(1).lower()
            day = int(match.group(2))
            
            if month_str in MONTH_NAMES:
                month = MONTH_NAMES[month_str]
                
                try:
                    target = date(reference_year, month, day)
                    
                    # If date is more than 30 days in the past, assume next year
                    days_diff = (target - today).days
                    if days_diff < -30:
                        target = date(reference_year + 1, month, day)
                    
                    return target
                except ValueError:
                    continue
    
    return None


# Legacy functions for backwards compatibility

def extract_temperature_range(question: str) -> Optional[Tuple[float, float]]:
    """
    Extract temperature range (tmin, tmax) from question text.
    Legacy function - use parse_temperature_question_v2 instead.
    """
    result = parse_temperature_question_v2(question)
    if result:
        return (result[0], result[1])
    return None


def extract_location(question: str) -> Optional[str]:
    """Legacy wrapper for extract_location_v2."""
    return extract_location_v2(question)


def parse_temperature_question(question: str, 
                                min_bucket_width: float = 0.5,
                                max_bucket_width: float = 50.0) -> Optional[Tuple[float, float, date, Optional[str]]]:
    """
    Parse a Polymarket temperature bucket question.
    Legacy function - returns (tmin_f, tmax_f, target_date, location).
    """
    result = parse_temperature_question_v2(question)
    if result:
        tmin_f, tmax_f, target_date, location, temp_unit, is_tail, tail_type = result
        
        # Skip validation for tail buckets
        if not is_tail:
            width = tmax_f - tmin_f
            if width < min_bucket_width or width > max_bucket_width:
                return None
        
        return (tmin_f, tmax_f, target_date, location)
    return None


def validate_bucket_width(tmin: float, tmax: float, 
                          min_width: float = 0.5, 
                          max_width: float = 10.0) -> bool:
    """
    Validate that bucket width is within acceptable range.
    """
    width = tmax - tmin
    return min_width <= width <= max_width


def format_bucket(tmin: float, tmax: float) -> str:
    """Format a bucket range for display."""
    # Handle tail buckets
    if tmin <= -900:
        return f"<={int(tmax)-1}F"
    if tmax >= 900:
        return f">={int(tmin)}F"
    
    if tmin == int(tmin) and tmax == int(tmax):
        return f"{int(tmin)}-{int(tmax)}F"
    return f"{tmin:.1f}-{tmax:.1f}F"


def buckets_are_contiguous(bucket1: Tuple[float, float], 
                           bucket2: Tuple[float, float],
                           tolerance: float = 0.1) -> bool:
    """
    Check if two buckets are contiguous (bucket1.tmax == bucket2.tmin).
    bucket1 should be the lower bucket.
    """
    return abs(bucket1[1] - bucket2[0]) < tolerance


def validate_bucket_group_consistency(buckets: list, tolerance: float = 0.5) -> Tuple[bool, Optional[float], List[str]]:
    """
    Validate that all buckets in a group have consistent widths.
    
    Args:
        buckets: List of (tmin, tmax) tuples
        tolerance: Maximum allowed deviation from mode width
    
    Returns:
        (is_consistent, mode_width, issues)
    """
    if not buckets:
        return True, None, []
    
    # Filter out tail buckets for width calculation
    regular_buckets = [(tmin, tmax) for tmin, tmax in buckets 
                       if tmin > -900 and tmax < 900]
    
    if not regular_buckets:
        return True, None, []
    
    # Calculate widths
    widths = [tmax - tmin for tmin, tmax in regular_buckets]
    
    # Find mode width (most common)
    from collections import Counter
    rounded_widths = [round(w, 1) for w in widths]
    width_counts = Counter(rounded_widths)
    mode_width = width_counts.most_common(1)[0][0]
    
    # Check all widths are within tolerance of mode
    issues = []
    for i, (tmin, tmax) in enumerate(regular_buckets):
        width = tmax - tmin
        if abs(width - mode_width) > tolerance:
            issues.append(f"Bucket {tmin:.1f}-{tmax:.1f} has width {width:.1f}, expected {mode_width:.1f}")
    
    is_consistent = len(issues) == 0
    return is_consistent, mode_width, issues


def filter_buckets_by_width(buckets: list, target_width: float, tolerance: float = 0.5) -> list:
    """
    Filter buckets to only include those matching target width.
    Tail buckets are always included.
    """
    result = []
    for tmin, tmax in buckets:
        # Keep tail buckets
        if tmin <= -900 or tmax >= 900:
            result.append((tmin, tmax))
            continue
        
        width = tmax - tmin
        if abs(width - target_width) <= tolerance:
            result.append((tmin, tmax))
    
    return result


def build_contiguous_intervals(buckets: list, max_width: float = 6.0) -> list:
    """
    Build all possible contiguous intervals from a list of buckets.
    
    Args:
        buckets: List of (tmin, tmax) tuples, sorted by tmin
        max_width: Maximum interval width in F
    
    Returns:
        List of intervals, each interval is a list of bucket indices
    """
    if not buckets:
        return []
    
    # Filter out tail buckets for interval building
    valid_buckets = [(i, b) for i, b in enumerate(buckets) 
                     if b[0] > -900 and b[1] < 900]
    
    if not valid_buckets:
        return []
    
    # Sort by tmin
    sorted_buckets = sorted(valid_buckets, key=lambda x: x[1][0])
    
    intervals = []
    n = len(sorted_buckets)
    
    for start_idx in range(n):
        original_idx, current_bucket = sorted_buckets[start_idx]
        interval = [original_idx]
        interval_tmin = current_bucket[0]
        interval_tmax = current_bucket[1]
        
        # Single bucket is a valid interval
        if interval_tmax - interval_tmin <= max_width:
            intervals.append(list(interval))
        
        # Try to extend the interval
        for next_idx in range(start_idx + 1, n):
            next_original_idx, next_bucket = sorted_buckets[next_idx]
            
            # Check if contiguous
            if not buckets_are_contiguous(current_bucket, next_bucket):
                break
            
            # Check width constraint
            new_tmax = next_bucket[1]
            if new_tmax - interval_tmin > max_width:
                break
            
            interval.append(next_original_idx)
            interval_tmax = new_tmax
            current_bucket = next_bucket
            
            intervals.append(list(interval))
    
    return intervals


if __name__ == "__main__":
    # Test parsing with actual Polymarket question formats
    test_questions = [
        # NYC Fahrenheit
        "Will the highest temperature in New York City be 27F or below on January 3?",
        "Will the highest temperature in New York City be between 28-29F on January 3?",
        "Will the highest temperature in New York City be between 30-31F on January 3?",
        "Will the highest temperature in New York City be 40F or higher on January 3?",
        # Seoul Celsius
        "Will the highest temperature in Seoul be -1C or below on January 3?",
        "Will the highest temperature in Seoul be 0C on January 3?",
        "Will the highest temperature in Seoul be 1C on January 3?",
        "Will the highest temperature in Seoul be 5C or higher on January 3?",
        # London Celsius
        "Will the highest temperature in London be -2C or below on January 3?",
        "Will the highest temperature in London be 0C on January 3?",
        # Atlanta Fahrenheit
        "Will the highest temperature in Atlanta be 59F or below on January 3?",
        "Will the highest temperature in Atlanta be between 60-61F on January 3?",
        "Will the highest temperature in Atlanta be 68F or higher on January 3?",
    ]
    
    logging.basicConfig(level=logging.DEBUG)
    
    print("Testing parse_temperature_question_v2:")
    print("=" * 80)
    
    for q in test_questions:
        result = parse_temperature_question_v2(q)
        print(f"\nQ: {q}")
        if result:
            tmin_f, tmax_f, target_date, location, unit, is_tail, tail_type = result
            bucket_str = format_bucket(tmin_f, tmax_f)
            tail_info = f" [{tail_type}]" if is_tail else ""
            print(f"  -> {bucket_str}{tail_info} (orig: {unit}) | {location} | {target_date}")
        else:
            print("  -> FAILED TO PARSE")
