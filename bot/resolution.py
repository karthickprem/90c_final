"""
Resolution logic for temperature bucket markets.
Maps observed temperature to winning bucket with correct boundary handling.
"""

import logging
from typing import List, Tuple, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class BucketDefinition:
    """Definition of a temperature bucket with resolution rules."""
    tmin_f: float
    tmax_f: float
    # Resolution semantics: [tmin, tmax) means >= tmin AND < tmax
    # This is the standard Polymarket convention for numeric ranges
    inclusive_min: bool = True   # tmin is included (>=)
    inclusive_max: bool = False  # tmax is excluded (<)
    
    def contains(self, temp_f: float) -> bool:
        """Check if temperature falls in this bucket."""
        if self.inclusive_min and self.inclusive_max:
            return self.tmin_f <= temp_f <= self.tmax_f
        elif self.inclusive_min and not self.inclusive_max:
            return self.tmin_f <= temp_f < self.tmax_f
        elif not self.inclusive_min and self.inclusive_max:
            return self.tmin_f < temp_f <= self.tmax_f
        else:
            return self.tmin_f < temp_f < self.tmax_f
    
    @property
    def bucket_str(self) -> str:
        return f"{self.tmin_f:.0f}-{self.tmax_f:.0f}F"


@dataclass
class MarketResolutionProfile:
    """
    Resolution source binding for a temperature market.
    Tracks the semantics needed for correct settlement.
    """
    # Station/location definition
    station_id: Optional[str] = None  # e.g., "EGLL" for Heathrow
    station_name: Optional[str] = None
    location_description: Optional[str] = None  # From market UI if available
    
    # Timezone and day boundary
    timezone: str = "UTC"  # Day boundary timezone
    day_start_hour: int = 0  # What hour starts the "day" (usually 0 or 6)
    
    # Temperature semantics
    temp_unit: str = "F"  # "F" or "C"
    metric_type: str = "daily_high"  # "daily_high", "daily_max", "metar_max", etc.
    
    # Bucket boundary convention
    inclusive_min: bool = True  # [min, max) is standard
    inclusive_max: bool = False
    
    # Confidence level
    source_known: bool = False  # If False, increase sigma for uncertainty
    
    @property
    def is_uncertain(self) -> bool:
        return not self.source_known


@dataclass
class LatticeValidation:
    """Result of lattice validation with soft metrics."""
    is_valid: bool
    coverage: float  # buckets_found / expected_buckets_in_range
    expected_buckets: int
    actual_buckets: int
    bucket_width: float
    tmin: float
    tmax: float
    gaps: List[Tuple[float, float]] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)


def winning_bucket(
    temp_f: float,
    buckets: List[Tuple[float, float]],
    inclusive_min: bool = True,
    inclusive_max: bool = False
) -> Optional[Tuple[float, float]]:
    """
    Determine which bucket wins given an observed temperature.
    
    Args:
        temp_f: Observed temperature in Fahrenheit
        buckets: List of (tmin, tmax) bucket definitions
        inclusive_min: Whether bucket includes tmin (>= vs >)
        inclusive_max: Whether bucket includes tmax (<= vs <)
    
    Returns:
        (tmin, tmax) of winning bucket, or None if no bucket matches.
    
    Resolution semantics (default Polymarket convention):
        Bucket "63-64F" wins if temp >= 63.0 AND temp < 64.0
        This means temp=63.0 -> bucket 63-64 wins
        And temp=64.0 -> bucket 64-65 wins
    
    Edge cases:
        - temp below all buckets: None (no winner)
        - temp above all buckets: None (no winner)
        - temp exactly on boundary: goes to higher bucket (with default settings)
    """
    if not buckets:
        return None
    
    # Sort buckets by tmin to ensure correct boundary handling
    sorted_buckets = sorted(buckets, key=lambda b: b[0])
    
    for tmin, tmax in sorted_buckets:
        bucket = BucketDefinition(
            tmin_f=tmin,
            tmax_f=tmax,
            inclusive_min=inclusive_min,
            inclusive_max=inclusive_max
        )
        if bucket.contains(temp_f):
            return (tmin, tmax)
    
    return None


def winning_bucket_index(
    temp_f: float,
    buckets: List[Tuple[float, float]],
    inclusive_min: bool = True,
    inclusive_max: bool = False
) -> Optional[int]:
    """
    Return the index of the winning bucket.
    Useful when you need to reference the original bucket list.
    """
    winner = winning_bucket(temp_f, buckets, inclusive_min, inclusive_max)
    if winner is None:
        return None
    
    for i, bucket in enumerate(buckets):
        if bucket[0] == winner[0] and bucket[1] == winner[1]:
            return i
    
    return None


def interval_hit(
    temp_f: float,
    interval_tmin: float,
    interval_tmax: float,
    inclusive_min: bool = True,
    inclusive_max: bool = False
) -> bool:
    """
    Check if temperature falls within an interval.
    
    For a contiguous interval of buckets [tmin, tmax), we check if
    the observed temp would make ANY bucket in that interval win.
    
    With default settings ([tmin, tmax) convention):
        interval 50-53 hits if temp >= 50 AND temp < 53
    """
    if inclusive_min and inclusive_max:
        return interval_tmin <= temp_f <= interval_tmax
    elif inclusive_min and not inclusive_max:
        return interval_tmin <= temp_f < interval_tmax
    elif not inclusive_min and inclusive_max:
        return interval_tmin < temp_f <= interval_tmax
    else:
        return interval_tmin < temp_f < interval_tmax


def celsius_to_fahrenheit(temp_c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return temp_c * 9 / 5 + 32


def fahrenheit_to_celsius(temp_f: float) -> float:
    """Convert Fahrenheit to Celsius."""
    return (temp_f - 32) * 5 / 9


def validate_bucket_lattice(buckets: List[Tuple[float, float]], 
                             expected_width: float = 1.0) -> LatticeValidation:
    """
    Validate buckets with soft metrics instead of hard rejection.
    
    Returns LatticeValidation with:
        - coverage: fraction of expected buckets found
        - gaps: list of missing bucket ranges
        - issues: list of problem descriptions
    """
    if not buckets:
        return LatticeValidation(
            is_valid=False, coverage=0.0, expected_buckets=0, actual_buckets=0,
            bucket_width=0, tmin=0, tmax=0, issues=["No buckets provided"]
        )
    
    sorted_buckets = sorted(buckets, key=lambda b: b[0])
    
    # Determine actual range and width
    tmin = sorted_buckets[0][0]
    tmax = sorted_buckets[-1][1]
    
    # Check bucket widths
    widths = [b[1] - b[0] for b in sorted_buckets]
    avg_width = sum(widths) / len(widths)
    width_variance = max(widths) - min(widths) if len(widths) > 1 else 0
    
    issues = []
    if width_variance > 0.1:
        issues.append(f"Inconsistent bucket widths: {min(widths):.1f} to {max(widths):.1f}")
    
    # Calculate expected buckets in range
    expected_buckets = int((tmax - tmin) / avg_width)
    actual_buckets = len(sorted_buckets)
    
    # Find gaps
    gaps = []
    for i in range(1, len(sorted_buckets)):
        prev_max = sorted_buckets[i-1][1]
        curr_min = sorted_buckets[i][0]
        
        gap = curr_min - prev_max
        if gap > 0.1:  # Significant gap
            gaps.append((prev_max, curr_min))
            issues.append(f"Gap: {prev_max:.0f}-{curr_min:.0f}")
    
    # Calculate coverage
    coverage = actual_buckets / expected_buckets if expected_buckets > 0 else 0
    
    # Soft validity check - at least 50% coverage is needed
    is_valid = coverage >= 0.5 and actual_buckets >= 2
    
    return LatticeValidation(
        is_valid=is_valid,
        coverage=coverage,
        expected_buckets=expected_buckets,
        actual_buckets=actual_buckets,
        bucket_width=avg_width,
        tmin=tmin,
        tmax=tmax,
        gaps=gaps,
        issues=issues
    )


def sanity_check_lattice_prices(
    buckets: List[Tuple[float, float]],
    prices: List[float],
    tolerance: float = 0.30  # Increased tolerance - don't hard reject
) -> Tuple[bool, str, float]:
    """
    Sanity check that bucket prices sum reasonably close to 1.0.
    
    This is now a WARNING, not a hard rejection.
    Thin markets may have stale prices that don't sum perfectly.
    
    Returns (is_reasonable, message, sum_price).
    """
    if len(buckets) != len(prices):
        return False, "Bucket/price count mismatch", 0.0
    
    total = sum(prices)
    
    # Very loose check - just flag obvious issues
    if total < 0.3:
        return False, f"Sum of prices {total:.3f} suspiciously low", total
    if total > 1.8:
        return False, f"Sum of prices {total:.3f} suspiciously high", total
    
    # Return True but note if it's off
    if abs(total - 1.0) > tolerance:
        return True, f"Sum {total:.3f} off from 1.0 (warning only)", total
    
    return True, f"Sum {total:.3f} reasonable", total


def check_interval_coverage(
    interval_buckets: List[Tuple[float, float]],
    all_buckets: List[Tuple[float, float]]
) -> Tuple[float, List[Tuple[float, float]]]:
    """
    Check how much of the interval is covered by available buckets.
    
    Returns (coverage_fraction, missing_buckets).
    """
    if not interval_buckets:
        return 0.0, []
    
    interval_min = min(b[0] for b in interval_buckets)
    interval_max = max(b[1] for b in interval_buckets)
    interval_width = interval_max - interval_min
    
    # Sum up width of buckets we have
    covered_width = sum(b[1] - b[0] for b in interval_buckets)
    coverage = covered_width / interval_width if interval_width > 0 else 0
    
    # Find missing buckets
    sorted_buckets = sorted(interval_buckets, key=lambda b: b[0])
    missing = []
    
    for i in range(1, len(sorted_buckets)):
        prev_max = sorted_buckets[i-1][1]
        curr_min = sorted_buckets[i][0]
        if curr_min > prev_max + 0.01:
            missing.append((prev_max, curr_min))
    
    return coverage, missing


def normalize_temperature(temp: float, unit: str = "F") -> float:
    """
    Normalize temperature to Fahrenheit.
    
    Args:
        temp: Temperature value
        unit: "F" for Fahrenheit, "C" for Celsius
    
    Returns:
        Temperature in Fahrenheit
    """
    unit = unit.upper()
    if unit == "C":
        return celsius_to_fahrenheit(temp)
    elif unit == "F":
        return temp
    else:
        raise ValueError(f"Unknown temperature unit: {unit}")


if __name__ == "__main__":
    # Test winning_bucket logic
    buckets = [
        (50, 51), (51, 52), (52, 53), (53, 54), (54, 55), (55, 56)
    ]
    
    print("Testing winning_bucket with [tmin, tmax) convention:")
    test_temps = [49.9, 50.0, 50.5, 51.0, 52.99, 53.0, 56.0, 57.0]
    
    for temp in test_temps:
        winner = winning_bucket(temp, buckets)
        print(f"  temp={temp:.2f}F -> winner={winner}")
    
    print("\nTesting interval_hit:")
    for temp in test_temps:
        hit = interval_hit(temp, 51, 54)
        print(f"  temp={temp:.2f}F in [51,54) -> {hit}")
    
    print("\nValidating bucket lattice (soft):")
    validation = validate_bucket_lattice(buckets)
    print(f"  Valid: {validation.is_valid}")
    print(f"  Coverage: {validation.coverage:.1%}")
    print(f"  Issues: {validation.issues}")
    
    # Test with gaps
    print("\nTesting with gaps:")
    gappy_buckets = [(50, 51), (52, 53), (53, 54)]
    validation = validate_bucket_lattice(gappy_buckets)
    print(f"  Valid: {validation.is_valid}")
    print(f"  Coverage: {validation.coverage:.1%}")
    print(f"  Gaps: {validation.gaps}")
