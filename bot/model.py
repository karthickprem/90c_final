"""
Probability model for daily high temperature.
Uses Normal distribution with calibrated sigma.
Includes Monte Carlo validation and auto-calibration.
"""

import logging
import math
from typing import List, Tuple, Optional, Dict
from datetime import date, datetime
from dataclasses import dataclass, field
import sqlite3

import numpy as np
from scipy import stats

from bot.weather import DailyForecast, WeatherProvider, get_weather_provider

logger = logging.getLogger(__name__)


@dataclass
class BucketProbability:
    """Probability estimate for a single bucket."""
    tmin_f: float
    tmax_f: float
    probability: float
    
    @property
    def bucket_str(self) -> str:
        return f"{self.tmin_f:.0f}-{self.tmax_f:.0f}F"


@dataclass
class IntervalProbability:
    """Probability estimate for an interval (contiguous buckets)."""
    tmin_f: float  # Lowest temp in interval
    tmax_f: float  # Highest temp in interval
    probability: float
    bucket_probs: List[BucketProbability]
    forecast_mu: float
    forecast_sigma: float
    # Calibration info
    sigma_raw: float = 0.0
    sigma_k: float = 1.0
    sigma_used: float = 0.0
    mc_validated: bool = False
    mc_probability: float = 0.0
    
    @property
    def interval_str(self) -> str:
        return f"{self.tmin_f:.0f}-{self.tmax_f:.0f}F"
    
    @property
    def width_f(self) -> float:
        return self.tmax_f - self.tmin_f


@dataclass
class CalibrationResult:
    """Result of sigma calibration."""
    k: float  # Multiplier on raw sigma
    n_samples: int
    neg_log_likelihood: float
    hit_rate_predicted: float
    hit_rate_actual: float
    calibration_ratio: float  # actual/predicted
    last_updated: datetime = field(default_factory=datetime.now)


class TemperatureModel:
    """
    Probability model for daily high temperature.
    
    Uses a Normal distribution centered on the forecast with
    uncertainty (sigma) that increases with forecast horizon.
    
    Key improvement: sigma is calibrated against historical accuracy.
    """
    
    # Monte Carlo validation settings
    MC_SAMPLES = 50000
    MC_TOLERANCE = 0.01
    
    def __init__(self, weather_provider: WeatherProvider = None, config: dict = None,
                 db_path: str = None):
        self.config = config or {}
        self.weather_provider = weather_provider or get_weather_provider(
            self.config.get("forecast_source", "open_meteo"),
            self.config
        )
        self.db_path = db_path or self.config.get("db_path", "bot_data.db")
        
        # Sigma calibration multiplier (k)
        # k > 1 means we underestimate uncertainty
        # k < 1 means we overestimate uncertainty
        self._sigma_k = self._load_calibration_k()
        
        # Raw sigma by horizon from config
        self._sigma_by_horizon = self.config.get("sigma_by_horizon", {
            0: 1.5, 1: 2.0, 2: 2.5, 3: 3.0, 4: 3.5, 5: 4.0, 6: 4.25, 7: 4.5
        })
        # Convert string keys to int if needed
        self._sigma_by_horizon = {int(k): v for k, v in self._sigma_by_horizon.items()}
    
    def _load_calibration_k(self) -> float:
        """Load calibration k from database, or return 1.0 if not calibrated."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("""
                SELECT k FROM calibration ORDER BY ts DESC LIMIT 1
            """)
            row = cursor.fetchone()
            conn.close()
            if row:
                return float(row[0])
        except Exception:
            pass
        return 1.0
    
    def _save_calibration(self, result: CalibrationResult):
        """Save calibration result to database."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS calibration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    k REAL NOT NULL,
                    n_samples INTEGER,
                    neg_log_likelihood REAL,
                    hit_rate_predicted REAL,
                    hit_rate_actual REAL,
                    calibration_ratio REAL,
                    ts TEXT NOT NULL
                )
            """)
            conn.execute("""
                INSERT INTO calibration 
                (k, n_samples, neg_log_likelihood, hit_rate_predicted, hit_rate_actual, calibration_ratio, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (result.k, result.n_samples, result.neg_log_likelihood,
                  result.hit_rate_predicted, result.hit_rate_actual,
                  result.calibration_ratio, result.last_updated.isoformat()))
            conn.commit()
            conn.close()
            logger.info(f"Saved calibration k={result.k:.3f}")
        except Exception as e:
            logger.error(f"Failed to save calibration: {e}")
    
    def get_sigma(self, days_ahead: int, apply_k: bool = True) -> Tuple[float, float, float]:
        """
        Get sigma for a given forecast horizon.
        
        Returns (sigma_raw, k, sigma_used) where:
            sigma_raw = base sigma from config
            k = calibration multiplier
            sigma_used = sigma_raw * k
        """
        # Get raw sigma from config
        if days_ahead in self._sigma_by_horizon:
            sigma_raw = self._sigma_by_horizon[days_ahead]
        elif days_ahead > max(self._sigma_by_horizon.keys()):
            sigma_raw = 5.0  # Cap for long-range
        else:
            sigma_raw = 2.5  # Default
        
        k = self._sigma_k if apply_k else 1.0
        sigma_used = sigma_raw * k
        
        return sigma_raw, k, sigma_used
    
    def get_forecast(self, location: str, target_date: date) -> Optional[DailyForecast]:
        """Get forecast for a location and date."""
        forecast = self.weather_provider.get_daily_forecast(location, target_date)
        
        if forecast:
            # Override sigma with calibrated value
            days_ahead = (target_date - date.today()).days
            _, _, sigma_used = self.get_sigma(max(0, days_ahead))
            forecast.uncertainty_sigma_f = sigma_used
        
        return forecast
    
    def bucket_probability(self, mu: float, sigma: float, 
                           tmin: float, tmax: float) -> float:
        """
        Calculate probability that temp falls in bucket [tmin, tmax).
        Uses CDF of Normal(mu, sigma).
        
        P(tmin <= T < tmax) = CDF(tmax) - CDF(tmin)
        """
        if sigma <= 0:
            sigma = 0.1  # Avoid division by zero
        
        dist = stats.norm(loc=mu, scale=sigma)
        prob = dist.cdf(tmax) - dist.cdf(tmin)
        return max(0.0, min(1.0, prob))
    
    def interval_probability(self, mu: float, sigma: float,
                              tmin: float, tmax: float) -> float:
        """
        Calculate probability that temp falls in interval [tmin, tmax).
        Same as bucket_probability but semantically for an interval.
        """
        return self.bucket_probability(mu, sigma, tmin, tmax)
    
    def monte_carlo_probability(self, mu: float, sigma: float,
                                 tmin: float, tmax: float,
                                 n_samples: int = None) -> float:
        """
        Monte Carlo estimate of interval probability.
        Used to validate CDF-based calculation.
        """
        n = n_samples or self.MC_SAMPLES
        samples = np.random.normal(mu, sigma, n)
        hits = np.sum((samples >= tmin) & (samples < tmax))
        return hits / n
    
    def validate_probability_mc(self, mu: float, sigma: float,
                                 tmin: float, tmax: float) -> Tuple[bool, float, float]:
        """
        Validate CDF probability against Monte Carlo.
        
        Returns (is_valid, p_cdf, p_mc).
        """
        p_cdf = self.interval_probability(mu, sigma, tmin, tmax)
        p_mc = self.monte_carlo_probability(mu, sigma, tmin, tmax)
        
        is_valid = abs(p_cdf - p_mc) < self.MC_TOLERANCE
        
        if not is_valid:
            logger.warning(f"MC validation failed: CDF={p_cdf:.4f}, MC={p_mc:.4f}")
        
        return is_valid, p_cdf, p_mc
    
    def get_bucket_probabilities(self, forecast: DailyForecast,
                                  buckets: List[Tuple[float, float]]) -> List[BucketProbability]:
        """
        Calculate probabilities for a list of buckets.
        """
        mu = forecast.high_temp_f
        sigma = forecast.uncertainty_sigma_f
        
        probs = []
        for tmin, tmax in buckets:
            prob = self.bucket_probability(mu, sigma, tmin, tmax)
            probs.append(BucketProbability(
                tmin_f=tmin,
                tmax_f=tmax,
                probability=prob
            ))
        
        return probs
    
    def get_interval_probability(self, forecast: DailyForecast,
                                   interval_tmin: float,
                                   interval_tmax: float,
                                   buckets: List[Tuple[float, float]] = None,
                                   validate_mc: bool = True) -> IntervalProbability:
        """
        Calculate probability for an interval with full calibration info.
        """
        mu = forecast.high_temp_f
        sigma = forecast.uncertainty_sigma_f
        
        # Get calibration info
        days_ahead = (forecast.target_date - date.today()).days
        sigma_raw, k, sigma_used = self.get_sigma(max(0, days_ahead))
        
        # Overall interval probability
        prob = self.interval_probability(mu, sigma, interval_tmin, interval_tmax)
        
        # Monte Carlo validation
        mc_validated = False
        mc_prob = 0.0
        if validate_mc:
            mc_validated, _, mc_prob = self.validate_probability_mc(
                mu, sigma, interval_tmin, interval_tmax
            )
        
        # Per-bucket breakdown if provided
        bucket_probs = []
        if buckets:
            bucket_probs = self.get_bucket_probabilities(forecast, buckets)
        
        return IntervalProbability(
            tmin_f=interval_tmin,
            tmax_f=interval_tmax,
            probability=prob,
            bucket_probs=bucket_probs,
            forecast_mu=mu,
            forecast_sigma=sigma,
            sigma_raw=sigma_raw,
            sigma_k=k,
            sigma_used=sigma_used,
            mc_validated=mc_validated,
            mc_probability=mc_prob
        )
    
    def calibrate_sigma(self, historical_data: List[Dict]) -> CalibrationResult:
        """
        Calibrate sigma multiplier (k) using historical forecast vs actual data.
        
        historical_data: List of dicts with:
            - forecast_mu: predicted daily high
            - forecast_sigma_raw: raw sigma used
            - actual_high: observed daily high
            - interval_tmin: interval we would have chosen
            - interval_tmax: interval we would have chosen
        
        We find k that minimizes negative log-likelihood of observed outcomes.
        """
        if len(historical_data) < 10:
            logger.warning("Not enough data for calibration (need 10+ samples)")
            return CalibrationResult(k=1.0, n_samples=len(historical_data),
                                    neg_log_likelihood=float('inf'),
                                    hit_rate_predicted=0, hit_rate_actual=0,
                                    calibration_ratio=0)
        
        def neg_log_likelihood(k: float) -> float:
            """Negative log-likelihood for a given k."""
            nll = 0.0
            for d in historical_data:
                mu = d["forecast_mu"]
                sigma = d["forecast_sigma_raw"] * k
                actual = d["actual_high"]
                
                # PDF at actual value
                prob = stats.norm(loc=mu, scale=sigma).pdf(actual)
                if prob > 0:
                    nll -= math.log(prob)
                else:
                    nll += 100  # Penalty for zero probability
            
            return nll
        
        # Grid search for optimal k
        best_k = 1.0
        best_nll = float('inf')
        
        for k in np.arange(0.5, 3.0, 0.1):
            nll = neg_log_likelihood(k)
            if nll < best_nll:
                best_nll = nll
                best_k = k
        
        # Fine-tune
        for k in np.arange(best_k - 0.1, best_k + 0.1, 0.01):
            nll = neg_log_likelihood(k)
            if nll < best_nll:
                best_nll = nll
                best_k = k
        
        # Calculate hit rates for diagnostics
        predicted_hits = 0.0
        actual_hits = 0
        
        for d in historical_data:
            mu = d["forecast_mu"]
            sigma = d["forecast_sigma_raw"] * best_k
            tmin, tmax = d["interval_tmin"], d["interval_tmax"]
            actual = d["actual_high"]
            
            # Predicted probability
            p = self.interval_probability(mu, sigma, tmin, tmax)
            predicted_hits += p
            
            # Actual hit
            if tmin <= actual < tmax:
                actual_hits += 1
        
        n = len(historical_data)
        hit_rate_predicted = predicted_hits / n
        hit_rate_actual = actual_hits / n
        calibration_ratio = hit_rate_actual / hit_rate_predicted if hit_rate_predicted > 0 else 0
        
        result = CalibrationResult(
            k=best_k,
            n_samples=n,
            neg_log_likelihood=best_nll,
            hit_rate_predicted=hit_rate_predicted,
            hit_rate_actual=hit_rate_actual,
            calibration_ratio=calibration_ratio
        )
        
        # Update internal k
        self._sigma_k = best_k
        
        # Save to database
        self._save_calibration(result)
        
        logger.info(f"Calibration complete: k={best_k:.3f}, ratio={calibration_ratio:.2f}")
        
        return result
    
    def print_calibration_stats(self, forecast: DailyForecast):
        """Print calibration statistics for debugging."""
        days_ahead = (forecast.target_date - date.today()).days
        sigma_raw, k, sigma_used = self.get_sigma(max(0, days_ahead))
        
        print(f"\n=== Model Calibration Stats ===")
        print(f"Forecast mu: {forecast.high_temp_f:.1f}F")
        print(f"Days ahead: {days_ahead}")
        print(f"Sigma raw: {sigma_raw:.2f}F")
        print(f"Calibration k: {k:.3f}")
        print(f"Sigma used: {sigma_used:.2f}F")
        
        # Show probability for a sample interval
        sample_tmin = forecast.high_temp_f - 2
        sample_tmax = forecast.high_temp_f + 2
        p_cdf = self.interval_probability(forecast.high_temp_f, sigma_used, sample_tmin, sample_tmax)
        p_mc = self.monte_carlo_probability(forecast.high_temp_f, sigma_used, sample_tmin, sample_tmax)
        
        print(f"\nSample interval [{sample_tmin:.0f}, {sample_tmax:.0f}):")
        print(f"  P(CDF): {p_cdf:.4f}")
        print(f"  P(MC):  {p_mc:.4f}")
        print(f"  MC validation: {'PASS' if abs(p_cdf - p_mc) < 0.01 else 'FAIL'}")


class CalibratedTemperatureModel(TemperatureModel):
    """
    Temperature model with auto-calibration.
    Requires historical data to be collected first.
    """
    
    def auto_calibrate(self, min_samples: int = 30) -> Optional[CalibrationResult]:
        """
        Auto-calibrate using stored historical data.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("""
                SELECT f.forecast_mu, f.forecast_sigma, r.observed_high_f,
                       p.interval_tmin, p.interval_tmax
                FROM forecasts f
                JOIN results r ON f.target_date = r.target_date
                JOIN positions p ON r.position_id = p.position_id
                WHERE r.observed_high_f IS NOT NULL
                ORDER BY f.ts_fetched DESC
                LIMIT ?
            """, (min_samples * 2,))
            
            rows = cursor.fetchall()
            conn.close()
            
            if len(rows) < min_samples:
                logger.warning(f"Only {len(rows)} samples, need {min_samples} for calibration")
                return None
            
            historical = []
            for row in rows:
                historical.append({
                    "forecast_mu": row[0],
                    "forecast_sigma_raw": row[1] / self._sigma_k,  # Undo previous k
                    "actual_high": row[2],
                    "interval_tmin": row[3],
                    "interval_tmax": row[4]
                })
            
            return self.calibrate_sigma(historical)
            
        except Exception as e:
            logger.error(f"Auto-calibration failed: {e}")
            return None


if __name__ == "__main__":
    # Test the probability model
    logging.basicConfig(level=logging.INFO)
    
    from datetime import timedelta
    
    model = TemperatureModel()
    tomorrow = date.today() + timedelta(days=1)
    
    # Get forecast
    forecast = model.get_forecast("London", tomorrow)
    if forecast:
        model.print_calibration_stats(forecast)
        
        # Test bucket probabilities
        buckets = [(50, 51), (51, 52), (52, 53), (53, 54), (54, 55)]
        probs = model.get_bucket_probabilities(forecast, buckets)
        
        print("\nBucket probabilities:")
        total = 0
        for bp in probs:
            print(f"  {bp.bucket_str}: {bp.probability:.4f} ({bp.probability*100:.2f}%)")
            total += bp.probability
        print(f"  Sum: {total:.4f}")
        
        # Test interval probability with MC validation
        print("\nInterval with MC validation:")
        interval = model.get_interval_probability(forecast, 51, 54, buckets[1:4], validate_mc=True)
        print(f"  Interval: {interval.interval_str}")
        print(f"  P(CDF): {interval.probability:.4f}")
        print(f"  P(MC): {interval.mc_probability:.4f}")
        print(f"  MC validated: {interval.mc_validated}")
        print(f"  Sigma: raw={interval.sigma_raw:.2f}, k={interval.sigma_k:.3f}, used={interval.sigma_used:.2f}")
    else:
        print("Could not get forecast")
