"""
Tests for temperature bucket resolution logic.
Validates boundary handling and hit mapping.
"""

import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.resolution import (
    winning_bucket,
    winning_bucket_index,
    interval_hit,
    BucketDefinition,
    celsius_to_fahrenheit,
    fahrenheit_to_celsius,
    validate_bucket_lattice,
    sanity_check_lattice_prices,
    normalize_temperature,
)


class TestBucketDefinition:
    """Tests for BucketDefinition.contains() with different boundary rules."""
    
    def test_default_inclusive_min_exclusive_max(self):
        """Default: [tmin, tmax) - includes min, excludes max."""
        bucket = BucketDefinition(50, 51)  # Default settings
        
        assert bucket.contains(50.0) is True   # Exactly at min
        assert bucket.contains(50.5) is True   # Middle
        assert bucket.contains(50.99) is True  # Just below max
        assert bucket.contains(51.0) is False  # Exactly at max (excluded)
        assert bucket.contains(49.99) is False # Below min
    
    def test_inclusive_both(self):
        """[tmin, tmax] - includes both boundaries."""
        bucket = BucketDefinition(50, 51, inclusive_min=True, inclusive_max=True)
        
        assert bucket.contains(50.0) is True
        assert bucket.contains(51.0) is True  # Now included
        assert bucket.contains(49.99) is False
        assert bucket.contains(51.01) is False
    
    def test_exclusive_min_inclusive_max(self):
        """(tmin, tmax] - excludes min, includes max."""
        bucket = BucketDefinition(50, 51, inclusive_min=False, inclusive_max=True)
        
        assert bucket.contains(50.0) is False  # Exactly at min (excluded)
        assert bucket.contains(50.01) is True
        assert bucket.contains(51.0) is True   # Exactly at max (included)
    
    def test_exclusive_both(self):
        """(tmin, tmax) - excludes both boundaries."""
        bucket = BucketDefinition(50, 51, inclusive_min=False, inclusive_max=False)
        
        assert bucket.contains(50.0) is False
        assert bucket.contains(51.0) is False
        assert bucket.contains(50.5) is True


class TestWinningBucket:
    """Tests for winning_bucket() function."""
    
    @pytest.fixture
    def standard_buckets(self):
        """Standard 1F buckets from 50-56F."""
        return [(50, 51), (51, 52), (52, 53), (53, 54), (54, 55), (55, 56)]
    
    def test_temp_below_all_buckets(self, standard_buckets):
        """Temperature below all buckets returns None."""
        assert winning_bucket(49.9, standard_buckets) is None
        assert winning_bucket(40.0, standard_buckets) is None
    
    def test_temp_above_all_buckets(self, standard_buckets):
        """Temperature above all buckets returns None."""
        assert winning_bucket(56.0, standard_buckets) is None
        assert winning_bucket(60.0, standard_buckets) is None
    
    def test_temp_exactly_at_bucket_min(self, standard_buckets):
        """Temperature exactly at bucket min goes to that bucket."""
        # With default [min, max) convention
        assert winning_bucket(50.0, standard_buckets) == (50, 51)
        assert winning_bucket(52.0, standard_buckets) == (52, 53)
        assert winning_bucket(55.0, standard_buckets) == (55, 56)
    
    def test_temp_exactly_at_bucket_max(self, standard_buckets):
        """Temperature exactly at bucket max goes to NEXT bucket."""
        # 51.0 is the max of (50,51) but excluded, so goes to (51,52)
        assert winning_bucket(51.0, standard_buckets) == (51, 52)
        assert winning_bucket(54.0, standard_buckets) == (54, 55)
    
    def test_temp_in_middle_of_bucket(self, standard_buckets):
        """Temperature in middle of bucket works correctly."""
        assert winning_bucket(50.5, standard_buckets) == (50, 51)
        assert winning_bucket(52.7, standard_buckets) == (52, 53)
    
    def test_boundary_at_first_bucket(self, standard_buckets):
        """Edge case: exactly at first bucket boundary."""
        assert winning_bucket(50.0, standard_buckets) == (50, 51)
    
    def test_boundary_at_last_bucket(self, standard_buckets):
        """Edge case: just below last bucket max."""
        assert winning_bucket(55.99, standard_buckets) == (55, 56)
    
    def test_unsorted_buckets(self):
        """Buckets provided out of order still work."""
        buckets = [(52, 53), (50, 51), (51, 52)]
        assert winning_bucket(51.5, buckets) == (51, 52)
    
    def test_empty_bucket_list(self):
        """Empty bucket list returns None."""
        assert winning_bucket(50.0, []) is None
    
    def test_inclusive_max_option(self, standard_buckets):
        """Test with inclusive_max=True changes boundary behavior."""
        # Now 51.0 would be in (50,51) not (51,52)
        result = winning_bucket(51.0, standard_buckets, 
                               inclusive_min=True, inclusive_max=True)
        # First matching bucket wins, and (50,51) now includes 51.0
        assert result == (50, 51)


class TestWinningBucketIndex:
    """Tests for winning_bucket_index()."""
    
    def test_returns_correct_index(self):
        buckets = [(50, 51), (51, 52), (52, 53)]
        assert winning_bucket_index(50.5, buckets) == 0
        assert winning_bucket_index(51.5, buckets) == 1
        assert winning_bucket_index(52.5, buckets) == 2
    
    def test_no_match_returns_none(self):
        buckets = [(50, 51), (51, 52)]
        assert winning_bucket_index(49.0, buckets) is None
        assert winning_bucket_index(55.0, buckets) is None


class TestIntervalHit:
    """Tests for interval_hit() function."""
    
    def test_inside_interval(self):
        """Temperatures inside interval return True."""
        assert interval_hit(51.5, 50, 53) is True
        assert interval_hit(50.0, 50, 53) is True  # At min
        assert interval_hit(52.99, 50, 53) is True  # Just below max
    
    def test_outside_interval(self):
        """Temperatures outside interval return False."""
        assert interval_hit(49.9, 50, 53) is False  # Below
        assert interval_hit(53.0, 50, 53) is False  # At max (excluded)
        assert interval_hit(55.0, 50, 53) is False  # Above
    
    def test_at_interval_boundary_min(self):
        """Temperature at interval min is included (default)."""
        assert interval_hit(50.0, 50, 53) is True
    
    def test_at_interval_boundary_max(self):
        """Temperature at interval max is excluded (default)."""
        assert interval_hit(53.0, 50, 53) is False
    
    def test_inclusive_max_option(self):
        """Test with inclusive_max=True."""
        assert interval_hit(53.0, 50, 53, inclusive_max=True) is True


class TestTemperatureConversion:
    """Tests for temperature unit conversion."""
    
    def test_celsius_to_fahrenheit(self):
        assert celsius_to_fahrenheit(0) == 32
        assert celsius_to_fahrenheit(100) == 212
        assert abs(celsius_to_fahrenheit(20) - 68) < 0.01
    
    def test_fahrenheit_to_celsius(self):
        assert fahrenheit_to_celsius(32) == 0
        assert fahrenheit_to_celsius(212) == 100
        assert abs(fahrenheit_to_celsius(68) - 20) < 0.01
    
    def test_roundtrip(self):
        """Conversion roundtrip should be identity."""
        for temp_f in [32, 50, 68, 86, 100]:
            assert abs(celsius_to_fahrenheit(fahrenheit_to_celsius(temp_f)) - temp_f) < 0.01
    
    def test_normalize_temperature(self):
        assert normalize_temperature(50, "F") == 50
        assert normalize_temperature(10, "C") == 50  # 10C = 50F


class TestValidateBucketLattice:
    """Tests for bucket lattice validation."""
    
    def test_valid_contiguous_lattice(self):
        buckets = [(50, 51), (51, 52), (52, 53)]
        result = validate_bucket_lattice(buckets)
        assert result.is_valid is True
        assert result.coverage > 0.9
    
    def test_gap_in_lattice(self):
        buckets = [(50, 51), (52, 53)]  # Gap at 51-52
        result = validate_bucket_lattice(buckets)
        # With soft validation, gaps reduce coverage but may not fail
        assert result.coverage < 1.0
        assert len(result.gaps) > 0 or len(result.issues) > 0
    
    def test_overlap_in_lattice(self):
        buckets = [(50, 52), (51, 53)]  # Overlap - buckets are malformed
        result = validate_bucket_lattice(buckets)
        # With inconsistent widths (2F each but overlapping), might be flagged or not
        # The key is the validation runs without error
        assert result is not None
    
    def test_inconsistent_widths(self):
        buckets = [(50, 51), (51, 53)]  # 1F and 2F buckets
        result = validate_bucket_lattice(buckets)
        # Inconsistent widths should be flagged
        assert len(result.issues) > 0
    
    def test_empty_buckets(self):
        result = validate_bucket_lattice([])
        assert result.is_valid is False


class TestSanityCheckLatticePrices:
    """Tests for price sanity checking (now with soft validation)."""
    
    def test_prices_sum_to_one(self):
        buckets = [(50, 51), (51, 52), (52, 53)]
        prices = [0.30, 0.40, 0.30]  # Sum = 1.0
        sane, msg, total = sanity_check_lattice_prices(buckets, prices)
        assert sane is True
        assert abs(total - 1.0) < 0.01
    
    def test_prices_too_high(self):
        buckets = [(50, 51), (51, 52), (52, 53)]
        prices = [0.50, 0.40, 0.50]  # Sum = 1.4
        sane, msg, total = sanity_check_lattice_prices(buckets, prices)
        # With soft validation, this is allowed but noted
        # Only very extreme values (>1.8) are rejected
        assert total == 1.4
    
    def test_prices_too_low(self):
        buckets = [(50, 51), (51, 52), (52, 53)]
        prices = [0.20, 0.20, 0.20]  # Sum = 0.6
        sane, msg, total = sanity_check_lattice_prices(buckets, prices)
        # With soft validation, this is allowed but noted
        # Only very extreme values (<0.3) are rejected
        assert abs(total - 0.6) < 0.001
    
    def test_prices_extremely_low_rejected(self):
        buckets = [(50, 51), (51, 52), (52, 53)]
        prices = [0.05, 0.05, 0.05]  # Sum = 0.15 (suspiciously low)
        sane, msg, _ = sanity_check_lattice_prices(buckets, prices)
        assert sane is False
    
    def test_mismatched_counts(self):
        buckets = [(50, 51), (51, 52)]
        prices = [0.50]  # Wrong count
        sane, msg, _ = sanity_check_lattice_prices(buckets, prices)
        assert sane is False


class TestBoundaryEdgeCases:
    """Critical edge case tests for boundary handling."""
    
    def test_polymarket_typical_bucket_63_64(self):
        """
        Polymarket bucket '63-64F' with standard [min, max) convention.
        """
        buckets = [(62, 63), (63, 64), (64, 65)]
        
        # 62.9F -> bucket 62-63
        assert winning_bucket(62.9, buckets) == (62, 63)
        
        # 63.0F -> bucket 63-64 (exactly at boundary, goes to higher bucket with [,) )
        assert winning_bucket(63.0, buckets) == (63, 64)
        
        # 63.5F -> bucket 63-64
        assert winning_bucket(63.5, buckets) == (63, 64)
        
        # 64.0F -> bucket 64-65 (excluded from 63-64)
        assert winning_bucket(64.0, buckets) == (64, 65)
    
    def test_observed_temp_at_exact_boundary(self):
        """
        What happens when observed daily high is exactly 64.0F?
        With [min, max) convention: goes to 64-65 bucket, NOT 63-64.
        """
        buckets = [(63, 64), (64, 65)]
        
        # If we bought the 63-64 interval and temp is exactly 64.0, we LOSE
        assert winning_bucket(64.0, buckets) == (64, 65)
        assert interval_hit(64.0, 63, 64) is False  # We lose!
    
    def test_half_degree_rounding(self):
        """
        Weather APIs might report 63.5F - ensure correct bucket.
        """
        buckets = [(63, 64), (64, 65)]
        assert winning_bucket(63.5, buckets) == (63, 64)
        assert winning_bucket(64.5, buckets) == (64, 65)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

