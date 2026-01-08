"""
Weather forecast adapters.
Fetches forecast data from public APIs (Open-Meteo, etc.)
"""

import logging
from typing import Dict, Optional, Tuple, List
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from abc import ABC, abstractmethod

import requests

logger = logging.getLogger(__name__)


@dataclass
class DailyForecast:
    """Forecast for a single day."""
    target_date: date
    location: str
    high_temp_f: float          # Forecasted daily max in °F
    low_temp_f: Optional[float] = None
    uncertainty_sigma_f: float = 2.0  # Standard deviation estimate
    forecast_time: Optional[datetime] = None
    source: str = "unknown"
    
    @property
    def high_temp_c(self) -> float:
        return (self.high_temp_f - 32) * 5 / 9
    
    @staticmethod
    def c_to_f(celsius: float) -> float:
        return celsius * 9 / 5 + 32
    
    @staticmethod
    def f_to_c(fahrenheit: float) -> float:
        return (fahrenheit - 32) * 5 / 9


@dataclass
class HourlyForecast:
    """Hourly forecast data for more detailed analysis."""
    timestamp: datetime
    temp_f: float
    humidity: Optional[float] = None
    wind_speed: Optional[float] = None


class WeatherProvider(ABC):
    """Abstract base class for weather providers."""
    
    @abstractmethod
    def get_daily_forecast(self, location: str, target_date: date) -> Optional[DailyForecast]:
        """Get daily forecast for a location and date."""
        pass
    
    @abstractmethod
    def get_hourly_forecast(self, location: str, target_date: date) -> List[HourlyForecast]:
        """Get hourly forecasts for a location and date."""
        pass


# Location coordinates for common cities
CITY_COORDINATES = {
    "london": (51.5074, -0.1278),
    "new york": (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298),
    "tokyo": (35.6762, 139.6503),
    "paris": (48.8566, 2.3522),
    "berlin": (52.5200, 13.4050),
    "sydney": (-33.8688, 151.2093),
    "mumbai": (19.0760, 72.8777),
    "beijing": (39.9042, 116.4074),
    # Additional cities for Polymarket temperature markets
    "atlanta": (33.7490, -84.3880),
    "seattle": (47.6062, -122.3321),
    "buenos aires": (-34.6037, -58.3816),
    "seoul": (37.5665, 126.9780),
    "toronto": (43.6532, -79.3832),
    "dallas": (32.7767, -96.7970),
    "miami": (25.7617, -80.1918),
    "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740),
    "denver": (39.7392, -104.9903),
    "boston": (42.3601, -71.0589),
    "san francisco": (37.7749, -122.4194),
}


class OpenMeteoProvider(WeatherProvider):
    """
    Weather provider using Open-Meteo API.
    Free, no API key required.
    https://open-meteo.com/
    """
    
    BASE_URL = "https://api.open-meteo.com/v1/forecast"
    
    def __init__(self, sigma_by_horizon: Dict[int, float] = None):
        self.session = requests.Session()
        # Default sigma (uncertainty) by days ahead
        self.sigma_by_horizon = sigma_by_horizon or {
            0: 1.5,   # Today
            1: 2.0,   # Tomorrow
            2: 2.5,
            3: 3.0,
            4: 3.5,
            5: 4.0,
            6: 4.25,
            7: 4.5,
        }
    
    def _get_coords(self, location: str) -> Tuple[float, float]:
        """Get latitude/longitude for a location."""
        loc_lower = location.lower().strip()
        if loc_lower in CITY_COORDINATES:
            return CITY_COORDINATES[loc_lower]
        raise ValueError(f"Unknown location: {location}. Add coordinates to CITY_COORDINATES.")
    
    def _get_sigma(self, days_ahead: int) -> float:
        """Get uncertainty estimate based on forecast horizon."""
        if days_ahead in self.sigma_by_horizon:
            return self.sigma_by_horizon[days_ahead]
        # Linear interpolation for days beyond configured
        if days_ahead > max(self.sigma_by_horizon.keys()):
            return 5.0  # Cap at 5°F for long-range
        return 2.5  # Default
    
    def get_daily_forecast(self, location: str, target_date: date) -> Optional[DailyForecast]:
        """
        Get daily forecast from Open-Meteo.
        Returns high temperature in Fahrenheit.
        """
        try:
            lat, lon = self._get_coords(location)
        except ValueError as e:
            logger.error(str(e))
            return None
        
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
        }
        
        try:
            response = self.session.get(self.BASE_URL, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            daily = data.get("daily", {})
            dates = daily.get("time", [])
            highs = daily.get("temperature_2m_max", [])
            lows = daily.get("temperature_2m_min", [])
            
            if not dates or not highs:
                logger.warning(f"No forecast data for {target_date}")
                return None
            
            # Find the matching date
            for i, d in enumerate(dates):
                if d == target_date.isoformat():
                    days_ahead = (target_date - date.today()).days
                    sigma = self._get_sigma(max(0, days_ahead))
                    
                    return DailyForecast(
                        target_date=target_date,
                        location=location,
                        high_temp_f=highs[i],
                        low_temp_f=lows[i] if i < len(lows) else None,
                        uncertainty_sigma_f=sigma,
                        forecast_time=datetime.now(),
                        source="open_meteo"
                    )
            
            logger.warning(f"Date {target_date} not found in response")
            return None
            
        except requests.RequestException as e:
            logger.error(f"Open-Meteo API error: {e}")
            return None
    
    def get_hourly_forecast(self, location: str, target_date: date) -> List[HourlyForecast]:
        """
        Get hourly forecasts for a location and date.
        Useful for Monte Carlo simulation of daily max.
        """
        try:
            lat, lon = self._get_coords(location)
        except ValueError as e:
            logger.error(str(e))
            return []
        
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
        }
        
        try:
            response = self.session.get(self.BASE_URL, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            temps = hourly.get("temperature_2m", [])
            humidity = hourly.get("relative_humidity_2m", [])
            wind = hourly.get("wind_speed_10m", [])
            
            forecasts = []
            for i, time_str in enumerate(times):
                try:
                    ts = datetime.fromisoformat(time_str)
                    forecasts.append(HourlyForecast(
                        timestamp=ts,
                        temp_f=temps[i] if i < len(temps) else 0,
                        humidity=humidity[i] if i < len(humidity) else None,
                        wind_speed=wind[i] if i < len(wind) else None,
                    ))
                except (ValueError, IndexError):
                    continue
            
            return forecasts
            
        except requests.RequestException as e:
            logger.error(f"Open-Meteo hourly API error: {e}")
            return []
    
    def get_multi_day_forecast(self, location: str, 
                                days_ahead: int = 7) -> List[DailyForecast]:
        """Get forecasts for multiple days ahead."""
        forecasts = []
        today = date.today()
        
        for i in range(days_ahead + 1):
            target = today + timedelta(days=i)
            forecast = self.get_daily_forecast(location, target)
            if forecast:
                forecasts.append(forecast)
        
        return forecasts


class MeteostatHistoricalProvider:
    """
    Provider for historical weather data using Meteostat.
    Used for backtesting and model calibration.
    """
    
    def __init__(self):
        self._meteostat_available = False
        try:
            from meteostat import Point, Daily
            self._meteostat_available = True
            self.Point = Point
            self.Daily = Daily
        except ImportError:
            logger.warning("Meteostat not installed. Historical data unavailable.")
    
    def get_historical_highs(self, location: str, 
                              start_date: date, 
                              end_date: date) -> Dict[date, float]:
        """
        Get historical daily high temperatures.
        Returns dict mapping date to high temp in °F.
        """
        if not self._meteostat_available:
            logger.error("Meteostat not available")
            return {}
        
        try:
            lat, lon = CITY_COORDINATES.get(location.lower(), (51.5074, -0.1278))
            point = self.Point(lat, lon)
            
            data = self.Daily(point, start_date, end_date)
            data = data.fetch()
            
            if data.empty:
                return {}
            
            # Convert Celsius to Fahrenheit
            result = {}
            for idx, row in data.iterrows():
                if 'tmax' in row and not pd.isna(row['tmax']):
                    # Meteostat returns Celsius
                    high_f = DailyForecast.c_to_f(row['tmax'])
                    result[idx.date()] = high_f
            
            return result
            
        except Exception as e:
            logger.error(f"Meteostat error: {e}")
            return {}


def get_weather_provider(source: str = "open_meteo", 
                          config: dict = None) -> WeatherProvider:
    """Factory function to get a weather provider by name."""
    if source == "open_meteo":
        sigma_config = None
        if config and "sigma_by_horizon" in config:
            sigma_config = {int(k): v for k, v in config["sigma_by_horizon"].items()}
        return OpenMeteoProvider(sigma_by_horizon=sigma_config)
    else:
        raise ValueError(f"Unknown weather provider: {source}")


if __name__ == "__main__":
    # Test weather providers
    logging.basicConfig(level=logging.INFO)
    
    provider = OpenMeteoProvider()
    
    # Test daily forecast
    today = date.today()
    tomorrow = today + timedelta(days=1)
    
    print("Testing Open-Meteo forecast for London...")
    forecast = provider.get_daily_forecast("London", tomorrow)
    if forecast:
        print(f"  Date: {forecast.target_date}")
        print(f"  High: {forecast.high_temp_f:.1f}°F")
        print(f"  Low: {forecast.low_temp_f:.1f}°F" if forecast.low_temp_f else "  Low: N/A")
        print(f"  Sigma: ±{forecast.uncertainty_sigma_f:.1f}°F")
    else:
        print("  No forecast available")
    
    # Test hourly forecast
    print("\nTesting hourly forecast...")
    hourly = provider.get_hourly_forecast("London", tomorrow)
    if hourly:
        print(f"  Got {len(hourly)} hourly readings")
        max_temp = max(h.temp_f for h in hourly)
        print(f"  Max from hourly: {max_temp:.1f}°F")
    else:
        print("  No hourly data")

