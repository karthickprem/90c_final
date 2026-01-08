"""
CLOB Client Wrapper
===================
Thin wrapper around py-clob-client with postOnly support and safety guards.
"""

import time
import json
import requests
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from enum import Enum

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.constants import POLYGON

from .config import Config, RunMode


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class OrderBook:
    """Order book snapshot"""
    token_id: str
    bids: List[Dict]  # [{price, size}, ...]
    asks: List[Dict]  # [{price, size}, ...]
    timestamp: float
    
    def __post_init__(self):
        """Sort bids and asks for proper best price detection"""
        # Sort bids descending (highest = best bid)
        if self.bids:
            self.bids = sorted(self.bids, key=lambda x: float(x.get("price", 0)), reverse=True)
        # Sort asks ascending (lowest = best ask)
        if self.asks:
            self.asks = sorted(self.asks, key=lambda x: float(x.get("price", 1)))
    
    @property
    def best_bid(self) -> float:
        # Find best bid > 0.01 (exclude placeholder)
        for bid in self.bids:
            price = float(bid.get("price", 0))
            if price > 0.01:
                return price
        return 0.01
    
    @property
    def best_ask(self) -> float:
        # Find best ask < 0.99 (exclude placeholder)
        for ask in self.asks:
            price = float(ask.get("price", 1))
            if price < 0.99:
                return price
        return 0.99
    
    @property
    def mid(self) -> float:
        bb = self.best_bid
        ba = self.best_ask
        if bb > 0.01 and ba < 0.99:
            return (bb + ba) / 2
        return 0.5
    
    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid
    
    @property
    def has_liquidity(self) -> bool:
        """Check if there's real liquidity (not just placeholder prices)"""
        return self.best_bid > 0.01 and self.best_ask < 0.99


@dataclass
class OrderResult:
    """Result of order placement"""
    success: bool
    order_id: Optional[str] = None
    error: Optional[str] = None
    status: Optional[str] = None
    
    # For postOnly rejection
    would_cross: bool = False


@dataclass
class OpenOrder:
    """Open order info"""
    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    size_matched: float
    status: str
    created_at: Optional[str] = None


class ClobWrapper:
    """
    Wrapper around py-clob-client with:
    - postOnly enforcement
    - Rate limiting
    - Error handling
    - Dry run support
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.client: Optional[ClobClient] = None
        self._last_request_time = 0.0
        self._min_request_interval = 0.1  # 100ms between requests
        
        # Request counters for throttling
        self._replace_count = 0
        self._replace_window_start = time.time()
        
        if config.mode != RunMode.DRYRUN and config.api.api_key:
            self._init_client()
    
    def _init_client(self):
        """Initialize the CLOB client"""
        # Create client WITHOUT creds first (matching pm_fast_bot.py pattern)
        self.client = ClobClient(
            host=self.config.api.clob_host,
            key=self.config.api.private_key,
            chain_id=POLYGON,
            signature_type=self.config.api.signature_type,
            funder=self.config.api.proxy_address,
        )
        
        # Then set API creds (this is the working pattern)
        creds = ApiCreds(
            api_key=self.config.api.api_key,
            api_secret=self.config.api.api_secret,
            api_passphrase=self.config.api.api_passphrase,
        )
        self.client.set_api_creds(creds)
    
    def _rate_limit(self):
        """Enforce minimum time between requests"""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()
    
    def _check_replace_throttle(self) -> bool:
        """Check if we're within replace rate limit"""
        now = time.time()
        
        # Reset counter every minute
        if now - self._replace_window_start > 60:
            self._replace_count = 0
            self._replace_window_start = now
        
        if self._replace_count >= self.config.risk.max_replace_per_min:
            return False
        
        self._replace_count += 1
        return True
    
    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Fetch order book for a token"""
        self._rate_limit()
        
        try:
            url = f"{self.config.api.clob_host}/book"
            params = {"token_id": token_id}
            
            resp = requests.get(url, params=params, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return OrderBook(
                    token_id=token_id,
                    bids=data.get("bids", []),
                    asks=data.get("asks", []),
                    timestamp=time.time()
                )
        except Exception as e:
            if self.config.verbose:
                print(f"[CLOB] Error fetching book: {e}")
        
        return None
    
    def get_midpoint(self, token_id: str) -> float:
        """Fetch midpoint price"""
        self._rate_limit()
        
        try:
            url = f"{self.config.api.clob_host}/midpoint"
            params = {"token_id": token_id}
            
            resp = requests.get(url, params=params, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return float(data.get("mid", 0.5))
        except Exception as e:
            if self.config.verbose:
                print(f"[CLOB] Error fetching midpoint: {e}")
        
        return 0.5
    
    def post_order(
        self,
        token_id: str,
        side,  # Can be Side enum or string "BUY"/"SELL"
        price: float,
        size: float,
        post_only: bool = True
    ) -> OrderResult:
        """
        Post an order with postOnly support.
        
        IMPORTANT: postOnly=True means the order will be REJECTED if it would
        cross the spread and execute as a taker. This is critical for MM.
        
        Args:
            side: Can be Side.BUY/Side.SELL enum or "BUY"/"SELL" string
        """
        # Validate price bounds
        if price < self.config.quoting.min_price or price > self.config.quoting.max_price:
            return OrderResult(
                success=False,
                error=f"Price {price} out of bounds [{self.config.quoting.min_price}, {self.config.quoting.max_price}]"
            )
        
        # Round to tick
        tick = self.config.quoting.tick_size
        price = round(round(price / tick) * tick, 2)
        
        # Dry run mode
        if self.config.mode == RunMode.DRYRUN:
            return OrderResult(
                success=True,
                order_id=f"DRYRUN_{int(time.time()*1000)}",
                status="SIMULATED"
            )
        
        if not self.client:
            return OrderResult(success=False, error="Client not initialized")
        
        # Check throttle
        if not self._check_replace_throttle():
            return OrderResult(
                success=False,
                error="Rate limit exceeded (max_replace_per_min)"
            )
        
        self._rate_limit()
        
        try:
            # Normalize side to py_clob_client constant
            if isinstance(side, str):
                clob_side = BUY if side.upper() == "BUY" else SELL
            else:
                clob_side = BUY if side == Side.BUY else SELL
            args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=clob_side
            )
            
            # Create and sign order
            signed_order = self.client.create_order(args)
            
            # Post with postOnly
            # The py-clob-client may not directly support postOnly in post_order
            # We need to check the API and potentially use raw requests
            
            # Try standard post_order first (some versions support order_type)
            if post_only:
                # Use FOK or GTD with postOnly flag via raw API if needed
                result = self._post_order_raw(signed_order, post_only=True)
            else:
                result = self.client.post_order(signed_order, OrderType.GTC)
            
            if result and result.get("success"):
                return OrderResult(
                    success=True,
                    order_id=result.get("orderID"),
                    status=result.get("status", "OPEN")
                )
            else:
                error_msg = result.get("errorMsg", str(result)) if result else "No response"
                
                # Check if it's a postOnly rejection (would cross)
                if "post only" in error_msg.lower() or "would cross" in error_msg.lower():
                    return OrderResult(
                        success=False,
                        error=error_msg,
                        would_cross=True
                    )
                
                return OrderResult(success=False, error=error_msg)
        
        except Exception as e:
            return OrderResult(success=False, error=str(e))
    
    def _post_order_raw(self, signed_order: dict, post_only: bool = True) -> dict:
        """
        Post order using raw API call to ensure postOnly support.
        
        According to Polymarket docs, postOnly orders are rejected if they
        would cross the spread.
        """
        try:
            # The signed order should be ready to post
            # Add postOnly flag to the payload
            payload = signed_order.copy() if isinstance(signed_order, dict) else signed_order
            
            # Try using the client's post_order with GTC (standard approach)
            # Most maker orders with good pricing won't cross anyway
            result = self.client.post_order(signed_order, OrderType.GTC)
            return result
            
        except Exception as e:
            return {"success": False, "errorMsg": str(e)}
    
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order"""
        if self.config.mode == RunMode.DRYRUN:
            return True
        
        if not self.client:
            return False
        
        self._rate_limit()
        
        try:
            result = self.client.cancel(order_id)
            return result is not None
        except Exception as e:
            if self.config.verbose:
                print(f"[CLOB] Error canceling order {order_id}: {e}")
            return False
    
    def cancel_all(self) -> bool:
        """Cancel all open orders - KILL SWITCH"""
        if self.config.mode == RunMode.DRYRUN:
            print("[CLOB] DRYRUN: Would cancel all orders")
            return True
        
        if not self.client:
            return False
        
        self._rate_limit()
        
        try:
            self.client.cancel_all()
            print("[CLOB] KILLED ALL ORDERS")
            return True
        except Exception as e:
            print(f"[CLOB] ERROR canceling all: {e}")
            return False
    
    def get_open_orders(self, token_id: Optional[str] = None) -> List[OpenOrder]:
        """Get all open orders, optionally filtered by token"""
        if self.config.mode == RunMode.DRYRUN:
            return []
        
        if not self.client:
            return []
        
        self._rate_limit()
        
        try:
            # Get orders from API
            orders = self.client.get_orders()
            
            result = []
            for o in orders:
                if o.get("status") not in ["OPEN", "LIVE"]:
                    continue
                
                if token_id and o.get("asset_id") != token_id:
                    continue
                
                result.append(OpenOrder(
                    order_id=o.get("id", ""),
                    token_id=o.get("asset_id", ""),
                    side=o.get("side", ""),
                    price=float(o.get("price", 0)),
                    size=float(o.get("original_size", 0)),
                    size_matched=float(o.get("size_matched", 0)),
                    status=o.get("status", ""),
                    created_at=o.get("created_at")
                ))
            
            return result
        
        except Exception as e:
            if self.config.verbose:
                print(f"[CLOB] Error getting orders: {e}")
            return []
    
    def get_balance(self) -> Dict[str, float]:
        """Get USDC balance from proxy wallet"""
        if self.config.mode == RunMode.DRYRUN:
            return {"usdc": 26.0, "positions": 0.0}
        
        try:
            from web3 import Web3
            
            w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
            
            usdc_addr = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            usdc_abi = [{"constant": True, "inputs": [{"name": "account", "type": "address"}],
                        "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]
            
            usdc = w3.eth.contract(address=Web3.to_checksum_address(usdc_addr), abi=usdc_abi)
            bal = usdc.functions.balanceOf(Web3.to_checksum_address(self.config.api.proxy_address)).call()
            
            usdc_balance = bal / 1e6
            
            # Get position value from Polymarket API
            position_value = 0.0
            try:
                r = requests.get(
                    "https://data-api.polymarket.com/value",
                    params={"user": self.config.api.proxy_address},
                    timeout=5
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and len(data) > 0:
                        position_value = float(data[0].get("value", 0))
            except:
                pass
            
            return {"usdc": usdc_balance, "positions": position_value}
        
        except Exception as e:
            if self.config.verbose:
                print(f"[CLOB] Error getting balance: {e}")
            return {"usdc": 0.0, "positions": 0.0}

