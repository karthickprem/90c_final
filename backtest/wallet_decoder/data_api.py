"""
Data API Client for Polymarket

Fetches trades and activity with:
- Pagination
- Exponential backoff retry
- Rate limiting
- Raw JSON caching to disk
"""

from __future__ import annotations

import json
import time
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import (
    TRADES_ENDPOINT,
    ACTIVITY_ENDPOINT,
    DEFAULT_LIMIT,
    DEFAULT_TIMEOUT,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
    DecoderConfig,
)


def create_session() -> requests.Session:
    """Create session with retry strategy."""
    session = requests.Session()
    
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=RETRY_BACKOFF_BASE,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    session.headers.update({
        "User-Agent": "WalletDecoder/1.0",
        "Accept": "application/json",
    })
    
    return session


class DataAPIClient:
    """
    Polymarket Data API client.
    
    READ-ONLY: Only GET requests to public data endpoints.
    """
    
    def __init__(self, config: DecoderConfig):
        self.config = config
        self.session = create_session()
        self.raw_dir = Path(config.outdir) / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
    
    def _fetch_paginated(
        self,
        endpoint: str,
        name: str,
        extra_params: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch all pages from an endpoint.
        
        Args:
            endpoint: Full URL
            name: For file naming (e.g., "trades", "activity")
            extra_params: Additional query params
        
        Returns:
            List of all records across pages
        """
        all_records = []
        offset = 0
        page = 0
        
        while page < self.config.max_pages:
            params = {
                "user": self.config.user_address,
                "limit": self.config.limit,
                "offset": offset,
            }
            
            if extra_params:
                params.update(extra_params)
            
            # Add date filters if supported (try them)
            if self.config.start_date:
                params["startDate"] = self.config.start_date.isoformat()
            if self.config.end_date:
                params["endDate"] = self.config.end_date.isoformat()
            
            if self.config.verbose:
                print(f"  Fetching {name} offset={offset}...")
            
            try:
                resp = self.session.get(
                    endpoint,
                    params=params,
                    timeout=DEFAULT_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                print(f"  ERROR fetching {name} at offset {offset}: {e}")
                break
            
            # Handle response format
            # Could be list directly or {"data": [...]}
            if isinstance(data, list):
                records = data
            elif isinstance(data, dict):
                records = data.get("data", data.get("results", data.get("trades", data.get("activity", []))))
                if isinstance(records, dict):
                    records = [records]
            else:
                records = []
            
            # Save raw page to disk
            page_file = self.raw_dir / f"{name}_offset_{offset}.json"
            with open(page_file, "w") as f:
                json.dump(records, f, indent=2, default=str)
            
            if not records:
                if self.config.verbose:
                    print(f"  {name}: No more records at offset {offset}")
                break
            
            all_records.extend(records)
            
            if self.config.verbose:
                print(f"  {name}: Got {len(records)} records (total: {len(all_records)})")
            
            if len(records) < self.config.limit:
                # Last page
                break
            
            offset += self.config.limit
            page += 1
            
            # Small delay to be polite
            time.sleep(0.1)
        
        return all_records
    
    def fetch_trades(self) -> List[Dict[str, Any]]:
        """Fetch all trades for the user."""
        print(f"Fetching trades for {self.config.user_address}...")
        return self._fetch_paginated(TRADES_ENDPOINT, "trades")
    
    def fetch_activity(self) -> List[Dict[str, Any]]:
        """Fetch all activity for the user."""
        print(f"Fetching activity for {self.config.user_address}...")
        return self._fetch_paginated(ACTIVITY_ENDPOINT, "activity")
    
    def fetch_all(self) -> tuple[List[Dict], List[Dict]]:
        """Fetch both trades and activity."""
        trades = self.fetch_trades()
        activity = self.fetch_activity()
        
        print(f"\nTotal: {len(trades)} trades, {len(activity)} activity events")
        
        return trades, activity


def apply_date_filter(
    records: List[Dict],
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    ts_field: str = "timestamp",
) -> List[Dict]:
    """
    Client-side date filtering if API doesn't support it.
    
    Args:
        records: List of records
        start_date: Filter start (inclusive)
        end_date: Filter end (inclusive)
        ts_field: Field name containing timestamp
    
    Returns:
        Filtered records
    """
    if not start_date and not end_date:
        return records
    
    filtered = []
    for r in records:
        ts_val = r.get(ts_field) or r.get("createdAt") or r.get("created_at")
        if not ts_val:
            # Keep records without timestamp
            filtered.append(r)
            continue
        
        # Parse timestamp
        try:
            if isinstance(ts_val, (int, float)):
                # Unix timestamp
                ts = datetime.fromtimestamp(ts_val)
            else:
                # ISO string
                ts_str = str(ts_val).replace("Z", "+00:00")
                ts = datetime.fromisoformat(ts_str.split("+")[0])
        except:
            filtered.append(r)
            continue
        
        if start_date and ts < start_date:
            continue
        if end_date and ts > end_date:
            continue
        
        filtered.append(r)
    
    return filtered


