"""
WebSocket User Channel Client
=============================
Subscribe to user channel for real-time fill updates.
"""

import json
import time
import asyncio
import threading
from typing import Callable, Optional, Dict, Any
from dataclasses import dataclass

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

from .config import Config


@dataclass
class FillEvent:
    """A fill event from websocket"""
    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    timestamp: float


@dataclass
class OrderEvent:
    """An order status event"""
    order_id: str
    status: str
    size_matched: float
    timestamp: float


class UserWebSocket:
    """
    WebSocket client for Polymarket user channel.
    
    Subscribes to:
    - Order updates
    - Fill events
    """
    
    def __init__(
        self,
        config: Config,
        on_fill: Optional[Callable[[FillEvent], None]] = None,
        on_order: Optional[Callable[[OrderEvent], None]] = None
    ):
        self.config = config
        self.on_fill = on_fill
        self.on_order = on_order
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Stats
        self.messages_received = 0
        self.fills_received = 0
        self.last_message_time = 0.0
    
    def start(self):
        """Start WebSocket connection in background thread"""
        if not HAS_WEBSOCKETS:
            print("[WS] websockets library not installed, skipping")
            return
        
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """Stop WebSocket connection"""
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
    
    def _run_loop(self):
        """Run asyncio event loop in thread"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        try:
            self._loop.run_until_complete(self._connect())
        except Exception as e:
            print(f"[WS] Error: {e}")
        finally:
            self._loop.close()
    
    async def _connect(self):
        """Connect and subscribe to user channel"""
        ws_url = self.config.api.ws_host
        
        while self._running:
            try:
                async with websockets.connect(ws_url) as ws:
                    print(f"[WS] Connected to {ws_url}")
                    
                    # Subscribe to user channel
                    # Note: Polymarket WS may require authentication
                    # This is a simplified version
                    sub_msg = {
                        "type": "subscribe",
                        "channel": "user",
                        "user": self.config.api.proxy_address
                    }
                    await ws.send(json.dumps(sub_msg))
                    
                    # Listen for messages
                    async for message in ws:
                        if not self._running:
                            break
                        
                        self._handle_message(message)
            
            except websockets.ConnectionClosed:
                print("[WS] Connection closed, reconnecting...")
                await asyncio.sleep(5)
            
            except Exception as e:
                print(f"[WS] Error: {e}")
                await asyncio.sleep(5)
    
    def _handle_message(self, raw: str):
        """Handle incoming WebSocket message"""
        self.messages_received += 1
        self.last_message_time = time.time()
        
        try:
            data = json.loads(raw)
            msg_type = data.get("type", "")
            
            if msg_type == "fill" or "fill" in str(data):
                self._handle_fill(data)
            
            elif msg_type == "order" or "order" in str(data):
                self._handle_order_update(data)
        
        except Exception as e:
            if self.config.verbose:
                print(f"[WS] Parse error: {e}")
    
    def _handle_fill(self, data: Dict[str, Any]):
        """Handle fill event"""
        self.fills_received += 1
        
        try:
            event = FillEvent(
                order_id=str(data.get("orderId", data.get("order_id", ""))),
                token_id=str(data.get("asset_id", data.get("tokenId", ""))),
                side=str(data.get("side", "")).upper(),
                price=float(data.get("price", 0)),
                size=float(data.get("size", data.get("matchedSize", 0))),
                timestamp=time.time()
            )
            
            if self.on_fill:
                self.on_fill(event)
        
        except Exception as e:
            if self.config.verbose:
                print(f"[WS] Fill parse error: {e}")
    
    def _handle_order_update(self, data: Dict[str, Any]):
        """Handle order status update"""
        try:
            event = OrderEvent(
                order_id=str(data.get("orderId", data.get("order_id", data.get("id", "")))),
                status=str(data.get("status", "")).upper(),
                size_matched=float(data.get("size_matched", data.get("matchedSize", 0))),
                timestamp=time.time()
            )
            
            if self.on_order:
                self.on_order(event)
        
        except Exception as e:
            if self.config.verbose:
                print(f"[WS] Order parse error: {e}")
    
    def get_stats(self) -> Dict:
        """Get WebSocket stats"""
        return {
            "running": self._running,
            "messages_received": self.messages_received,
            "fills_received": self.fills_received,
            "last_message_time": self.last_message_time
        }

