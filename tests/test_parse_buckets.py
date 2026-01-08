"""
Tests for bucket parsing functionality.
"""

import pytest
from datetime import date

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.parse_buckets import (
    extract_temperature_range,
    extract_date,
    extract_location,
    parse_temperature_question,
    validate_bucket_width,
    format_bucket,
    buckets_are_contiguous,
    build_contiguous_intervals,
)


class TestExtractTemperatureRange:
    """Tests for temperature range extraction."""
    
    def test_simple_dash_range(self):
        """Test parsing '63-64°F' format."""
        result = extract_temperature_range("Will it be 63-64°F tomorrow?")
        assert result == (63.0, 64.0)
    
    def test_en_dash_range(self):
        """Test parsing with en-dash '63–64°F'."""
        result = extract_temperature_range("Temperature 50–51°F on Monday")
        assert result == (50.0, 51.0)
    
    def test_between_and_format(self):
        """Test 'between X and Y°F' format."""
        result = extract_temperature_range("Will temp be between 45 and 46°F?")
        assert result == (45.0, 46.0)
    
    def test_from_to_format(self):
        """Test 'from X to Y°F' format."""
        result = extract_temperature_range("Temperature from 70 to 72°F expected")
        assert result == (70.0, 72.0)
    
    def test_x_to_y_format(self):
        """Test 'X to Y°F' format without 'from'."""
        result = extract_temperature_range("High of 55 to 56°F")
        assert result == (55.0, 56.0)
    
    def test_decimal_temperatures(self):
        """Test decimal temperature values."""
        result = extract_temperature_range("Range 63.5-64.5°F")
        assert result == (63.5, 64.5)
    
    def test_no_range_returns_none(self):
        """Test that no range returns None."""
        result = extract_temperature_range("It will be warm tomorrow")
        assert result is None
    
    def test_invalid_range_reversed(self):
        """Test that reversed range (max < min) returns None."""
        result = extract_temperature_range("Temperature 70-65°F")
        assert result is None
    
    def test_space_around_dash(self):
        """Test with spaces around dash."""
        result = extract_temperature_range("Will it be 63 - 64 °F?")
        assert result == (63.0, 64.0)


class TestExtractDate:
    """Tests for date extraction."""
    
    def test_month_day_format(self):
        """Test 'January 15' format."""
        result = extract_date("Temperature on January 15")
        assert result is not None
        assert result.month == 1
        assert result.day == 15
    
    def test_abbrev_month_day(self):
        """Test 'Jan 15' format."""
        result = extract_date("High on Jan 20")
        assert result is not None
        assert result.month == 1
        assert result.day == 20
    
    def test_day_month_format(self):
        """Test '15th January' format."""
        result = extract_date("Temperature 15th January")
        assert result is not None
        assert result.month == 1
        assert result.day == 15
    
    def test_ordinal_suffix(self):
        """Test ordinal suffixes (1st, 2nd, 3rd, 4th)."""
        for day, suffix in [(1, "st"), (2, "nd"), (3, "rd"), (4, "th"), (21, "st")]:
            result = extract_date(f"Temperature on January {day}{suffix}")
            assert result is not None
            assert result.day == day
    
    def test_no_date_returns_none(self):
        """Test that missing date returns None."""
        result = extract_date("Temperature will be warm")
        assert result is None
    
    def test_future_date_adjustment(self):
        """Test that past dates roll to next year."""
        today = date.today()
        # Use a date that's definitely in the past this year
        past_month = (today.month - 2) % 12 or 12
        result = extract_date(f"Temperature on {date(2000, past_month, 15).strftime('%B')} 15")
        if result:
            # Should be in the future
            assert result >= today


class TestExtractLocation:
    """Tests for location extraction."""
    
    def test_london(self):
        """Test extracting 'London'."""
        result = extract_location("Temperature in London tomorrow")
        assert result == "London"
    
    def test_new_york(self):
        """Test extracting 'New York'."""
        result = extract_location("What's the high in New York?")
        assert result == "New York"
    
    def test_case_insensitive(self):
        """Test case insensitivity."""
        result = extract_location("Temperature in LONDON today")
        assert result == "London"
    
    def test_no_location_returns_none(self):
        """Test missing location returns None."""
        result = extract_location("Temperature will be warm")
        assert result is None


class TestParseTemperatureQuestion:
    """Tests for full question parsing."""
    
    def test_full_polymarket_format(self):
        """Test typical Polymarket question format."""
        question = "Will the highest temperature in London be between 63-64°F on January 15?"
        result = parse_temperature_question(question)
        
        assert result is not None
        tmin, tmax, target_date, location = result
        assert tmin == 63.0
        assert tmax == 64.0
        assert target_date.month == 1
        assert target_date.day == 15
        assert location == "London"
    
    def test_alternative_format(self):
        """Test alternative question format."""
        question = "London daily high: 50–51°F on Jan 20, 2026?"
        result = parse_temperature_question(question)
        
        assert result is not None
        tmin, tmax, target_date, location = result
        assert tmin == 50.0
        assert tmax == 51.0
        assert location == "London"
    
    def test_from_to_format(self):
        """Test 'from X to Y' format."""
        question = "Will the temperature in London be from 45 to 46°F on February 3?"
        result = parse_temperature_question(question)
        
        assert result is not None
        tmin, tmax, target_date, location = result
        assert tmin == 45.0
        assert tmax == 46.0
    
    def test_missing_temp_returns_none(self):
        """Test that missing temperature returns None."""
        question = "Will it rain in London on January 15?"
        result = parse_temperature_question(question)
        assert result is None
    
    def test_missing_date_returns_none(self):
        """Test that missing date returns None."""
        question = "Will the temperature in London be 63-64°F?"
        result = parse_temperature_question(question)
        assert result is None
    
    def test_bucket_width_validation(self):
        """Test that very wide buckets are rejected."""
        question = "Temperature in London 50-70°F on Jan 15?"  # 20°F bucket
        result = parse_temperature_question(question, max_bucket_width=5.0)
        assert result is None


class TestValidateBucketWidth:
    """Tests for bucket width validation."""
    
    def test_valid_1f_bucket(self):
        """Test 1°F bucket is valid."""
        assert validate_bucket_width(50.0, 51.0) is True
    
    def test_valid_2f_bucket(self):
        """Test 2°F bucket is valid."""
        assert validate_bucket_width(50.0, 52.0) is True
    
    def test_too_narrow_bucket(self):
        """Test too narrow bucket is invalid."""
        assert validate_bucket_width(50.0, 50.3, min_width=0.5) is False
    
    def test_too_wide_bucket(self):
        """Test too wide bucket is invalid."""
        assert validate_bucket_width(50.0, 60.0, max_width=5.0) is False


class TestFormatBucket:
    """Tests for bucket formatting."""
    
    def test_integer_temps(self):
        """Test formatting integer temperatures."""
        assert format_bucket(50.0, 51.0) == "50-51°F"
    
    def test_decimal_temps(self):
        """Test formatting decimal temperatures."""
        assert format_bucket(50.5, 51.5) == "50.5-51.5°F"


class TestBucketsAreContiguous:
    """Tests for contiguity checking."""
    
    def test_contiguous_buckets(self):
        """Test contiguous buckets are detected."""
        assert buckets_are_contiguous((50, 51), (51, 52)) is True
    
    def test_non_contiguous_buckets(self):
        """Test non-contiguous buckets are detected."""
        assert buckets_are_contiguous((50, 51), (52, 53)) is False
    
    def test_overlapping_buckets(self):
        """Test overlapping buckets are not contiguous."""
        assert buckets_are_contiguous((50, 52), (51, 53)) is False


class TestBuildContiguousIntervals:
    """Tests for interval building."""
    
    def test_single_bucket(self):
        """Test single bucket creates one interval."""
        buckets = [(50, 51)]
        intervals = build_contiguous_intervals(buckets, max_width=6)
        assert len(intervals) == 1
        assert intervals[0] == [0]
    
    def test_two_contiguous_buckets(self):
        """Test two contiguous buckets create 3 intervals."""
        buckets = [(50, 51), (51, 52)]
        intervals = build_contiguous_intervals(buckets, max_width=6)
        # Should have: [0], [1], [0,1]
        assert len(intervals) == 3
    
    def test_three_contiguous_buckets(self):
        """Test three contiguous buckets create correct intervals."""
        buckets = [(50, 51), (51, 52), (52, 53)]
        intervals = build_contiguous_intervals(buckets, max_width=6)
        # [0], [0,1], [0,1,2], [1], [1,2], [2]
        assert len(intervals) == 6
    
    def test_max_width_constraint(self):
        """Test max width constraint is enforced."""
        buckets = [(50, 51), (51, 52), (52, 53), (53, 54)]
        intervals = build_contiguous_intervals(buckets, max_width=2)
        # No interval should span more than 2°F
        for interval in intervals:
            bucket_list = [buckets[i] for i in interval]
            width = bucket_list[-1][1] - bucket_list[0][0]
            assert width <= 2
    
    def test_non_contiguous_gap(self):
        """Test non-contiguous buckets don't form intervals."""
        buckets = [(50, 51), (52, 53)]  # Gap at 51-52
        intervals = build_contiguous_intervals(buckets, max_width=6)
        # Should only have [0] and [1], not [0,1]
        assert len(intervals) == 2
    
    def test_unsorted_input(self):
        """Test unsorted input is handled correctly."""
        buckets = [(52, 53), (50, 51), (51, 52)]  # Out of order
        intervals = build_contiguous_intervals(buckets, max_width=6)
        # Should still find contiguous intervals
        assert len(intervals) == 6  # Same as sorted case


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

