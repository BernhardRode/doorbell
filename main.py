#!/usr/bin/env python3
"""
Doorbell controller (single file):
- Event bus with structured logging
- Active state timer (LED follows active state)
- GPIO: buttons + LED, polling in a background thread with debouncing
- Camera fetcher with a lazy cache for images
- HTTP API (Starlette + uvicorn): /status, /image, /video, /healthz
- UDP:
   * Listener (commands: PING, STATUS, BELL <n>, ACTIVATE [seconds])
   * Optional heartbeat broadcast with active state and uptime

Run:
  uvicorn main:app --host 0.0.0.0 --port 8000

Environment variables:
  AXIS_HOSTNAME         default "192.168.1.20"
  AXIS_USERNAME         default "loxone"
  AXIS_PASSWORD         default "password"
  ACTIVE_TIMEOUT        default 10 (seconds)
  BUTTON_PINS           default "16,20,21" (BCM)
  BUTTON_DEBOUNCE_MS    default 200 (milliseconds)
  LED_PIN               default "17" (BCM)
  IMAGE_CACHE_TTL_SECS  default 2 (seconds for lazy cache)
  UDP_LISTEN_PORT       default 9999 (0 to disable listener)
  UDP_BROADCAST         default "0" (set "1" to enable)
  UDP_BCAST_ADDR        default "255.255.255.255"
  UDP_BCAST_PORT        default 9999
  UDP_BCAST_SECS        default 5
  LOG_LEVEL             default "INFO" (DEBUG for more)
"""
from __future__ import annotations

import logging
import re

# =============================================================================
# Credential Sanitization (MUST BE FIRST)
# =============================================================================
class SanitizeFilter(logging.Filter):
    def filter(self, record):
        if hasattr(record, 'msg'):
            msg = str(record.msg)
            record.msg = re.sub(r'://[^:]+:[^@]+@', '://***:***@', msg)
        return True

# Apply filter immediately to catch all logs
logging.getLogger().addFilter(SanitizeFilter())

import asyncio
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, List

import httpx
from starlette.applications import Starlette
from starlette.responses import (JSONResponse, PlainTextResponse, Response,
                                 StreamingResponse)
from starlette.routing import Route

from pysip_server import SIPServer

# --- GPIO import (optionally stubbed for non-Pi dev) --------------------------
GPIO_FAKE = os.environ.get("GPIO_FAKE", "0") == "1"
try:
    if GPIO_FAKE:
        raise ImportError("Using fake GPIO as requested")
    import RPi.GPIO as GPIO  # type: ignore
except (ImportError, RuntimeError):
    class _FakeGPIO:
        BCM = "BCM"
        IN, OUT = "IN", "OUT"
        PUD_UP = "PUD_UP"
        HIGH, LOW = 1, 0
        _pins = {}
        def setwarnings(self, *_): pass
        def setmode(self, *_): pass
        def setup(self, pin, mode, **kw): self._pins[pin] = kw.get("initial", self.LOW)
        def input(self, pin): return self._pins.get(pin, self.HIGH)
        def output(self, pin, val): self._pins[pin] = val
        def cleanup(self): pass
    GPIO = _FakeGPIO()
    print("WARNING: Using fake GPIO interface.")


# =============================================================================
# Configuration
# =============================================================================
# =============================================================================
# Configuration
# =============================================================================
def load_config() -> dict:
    """Load configuration from XDG config directories"""
    config = {}
    
    # XDG config directories
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    xdg_config_dirs = os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":")
    
    config_paths = [
        Path(xdg_config_home) / "doorbell" / "config.json",
        Path(xdg_config_home) / "doorbell.json",
    ]
    
    # Add system config paths
    for config_dir in xdg_config_dirs:
        config_paths.extend([
            Path(config_dir) / "doorbell" / "config.json",
            Path(config_dir) / "doorbell.json",
        ])
    
    # Load first existing config file
    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = json.load(f)
                print(f"Loaded config from {config_path}")
                break
            except Exception as e:
                print(f"Failed to load config from {config_path}: {e}")
    
    return config

@dataclass(frozen=True)
class Settings:
    def __init__(self):
        # Load config file first
        config = load_config()
        
        # Environment variables override config file
        object.__setattr__(self, 'AXIS_HOSTNAME', os.environ.get("AXIS_HOSTNAME", config.get("axis_hostname", "192.168.1.20")))
        object.__setattr__(self, 'AXIS_USERNAME', os.environ.get("AXIS_USERNAME", config.get("axis_username", "loxone")))
        object.__setattr__(self, 'AXIS_PASSWORD', os.environ.get("AXIS_PASSWORD", config.get("axis_password", "password")))
        object.__setattr__(self, 'ACTIVE_TIMEOUT', int(os.environ.get("ACTIVE_TIMEOUT", config.get("active_timeout", "10"))))
        
        button_pins_str = os.environ.get("BUTTON_PINS", config.get("button_pins", "16,20,21"))
        if isinstance(button_pins_str, list):
            button_pins = button_pins_str
        else:
            button_pins = [int(p) for p in str(button_pins_str).split(",") if p.strip()]
        object.__setattr__(self, 'BUTTON_PINS', button_pins)
        
        object.__setattr__(self, 'BUTTON_DEBOUNCE_MS', int(os.environ.get("BUTTON_DEBOUNCE_MS", config.get("button_debounce_ms", "200"))))
        object.__setattr__(self, 'LED_PIN', int(os.environ.get("LED_PIN", config.get("led_pin", "17"))))
        object.__setattr__(self, 'IMAGE_CACHE_TTL_SECS', int(os.environ.get("IMAGE_CACHE_TTL_SECS", config.get("image_cache_ttl_secs", "2"))))
        object.__setattr__(self, 'UDP_LISTEN_PORT', int(os.environ.get("UDP_LISTEN_PORT", config.get("udp_listen_port", "9999"))))
        object.__setattr__(self, 'UDP_BROADCAST_ENABLED', os.environ.get("UDP_BROADCAST", str(config.get("udp_broadcast_enabled", False))).lower() in ("1", "true"))
        object.__setattr__(self, 'UDP_BCAST_ADDR', os.environ.get("UDP_BCAST_ADDR", config.get("udp_bcast_addr", "255.255.255.255")))
        object.__setattr__(self, 'UDP_BCAST_PORT', int(os.environ.get("UDP_BCAST_PORT", config.get("udp_bcast_port", "9999"))))
        object.__setattr__(self, 'UDP_BCAST_INTERVAL', int(os.environ.get("UDP_BCAST_SECS", config.get("udp_bcast_interval", "5"))))
        object.__setattr__(self, 'LOG_LEVEL', os.environ.get("LOG_LEVEL", config.get("log_level", "INFO")).upper())
        object.__setattr__(self, 'LOG_FILE', os.environ.get("LOG_FILE", config.get("log_file", None)))
        print(f"DEBUG: LOG_FILE from env: {os.environ.get("LOG_FILE")}")
        print(f"DEBUG: LOG_FILE from config: {config.get("log_file")}")
        print(f"DEBUG: Final LOG_FILE setting: {self.LOG_FILE}")
        
        # SIP Configuration
        object.__setattr__(self, 'SIP_PORT', int(os.environ.get("SIP_PORT", config.get("sip_port", "5060"))))
        object.__setattr__(self, 'SIP_USER', os.environ.get("SIP_USER", config.get("sip_user", "door")))
        object.__setattr__(self, 'SIP_DOMAIN', os.environ.get("SIP_DOMAIN", config.get("sip_domain", "doorbell.local")))
        object.__setattr__(self, 'SIP_ENABLED', os.environ.get("SIP_ENABLED", str(config.get("sip_enabled", True))).lower() in ("1", "true"))

settings = Settings()

# =============================================================================
# Logging
# =============================================================================
class LogFormatter(logging.Formatter):
    def format(self, record):
        # Replace confusing logger names
        if record.name == "uvicorn.error":
            record.name = "uvicorn"
        elif record.name == "uvicorn.access":
            record.name = "access"
        return super().format(record)

# --- MODIFICATION: Added force=True to ensure this config takes precedence.
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Apply custom formatter to all handlers
for handler in logging.getLogger().handlers:
    handler.setFormatter(LogFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

# --- NEW: Add file handler if LOG_FILE is specified
if settings.LOG_FILE:
    file_handler = logging.FileHandler(settings.LOG_FILE)
    file_handler.setFormatter(LogFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(file_handler)

logger = logging.getLogger("doorbell")
START_TIME = time.time()


# =============================================================================
# Event Bus
# =============================================================================
class EventBus:
    def __init__(self):
        self._subscribers: dict[str, list[Callable[..., Awaitable[None]]]] = {}

    def subscribe(self, event: str, handler: Callable[..., Awaitable[None]]):
        logger.debug(f"Subscribed {getattr(handler, '__name__', str(handler))} to '{event}'")
        self._subscribers.setdefault(event, []).append(handler)

    async def emit(self, event: str, *args, **kwargs):
        handlers = self._subscribers.get(event, [])
        logger.debug(f"Emitting '{event}' to {len(handlers)} handlers")
        for handler in handlers:
            try:
                await handler(*args, **kwargs)
            except Exception:
                logger.exception(f"Handler {getattr(handler,'__name__',str(handler))} failed for event '{event}'")


# =============================================================================
# Active State Manager
# =============================================================================
class ActiveState:
    def __init__(self, event_bus: EventBus, default_timeout: int):
        self._event_bus = event_bus
        self._default_timeout = default_timeout
        self._active_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._is_active = False
        self._activated_at = 0.0

    @property
    def is_active(self) -> bool: 
        # Stay active if there's an ongoing call
        if sip_server and sip_server.is_call_active:
            return True
        return self._is_active
    
    @property
    def activated_at(self) -> float: return self._activated_at

    async def handle_bell_pressed(self, button_number: int):
        logger.info(f"Bell pressed event received for button {button_number}")
        await self.activate()

    async def activate(self, seconds: int | None = None):
        duration = seconds if seconds is not None and seconds > 0 else self._default_timeout
        logger.info(f"Activating for {duration} seconds")
        async with self._lock:
            if self._active_task and not self._active_task.done():
                logger.debug("Extending active mode")
                self._active_task.cancel()
            self._active_task = asyncio.create_task(self._active_mode_task(duration))

    async def _active_mode_task(self, duration: int):
        if not self._is_active:
            self._is_active = True
            self._activated_at = time.time()
            logger.info("Active mode START")
            await self._event_bus.emit("ACTIVE_STARTED")
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            logger.debug("Active mode timer extended")
            return
        
        # Check if we should stay active due to ongoing call
        while self._sip_server and self._sip_server.is_call_active:
            logger.debug("Staying active due to ongoing SIP call")
            await asyncio.sleep(1)
        
        # This part only runs if sleep completes without cancellation and no active call
        self._is_active = False
        logger.info("Active mode END")
        await self._event_bus.emit("ACTIVE_ENDED")

    async def shutdown(self):
        logger.debug("Shutting down active state manager")
        async with self._lock:
            if self._active_task and not self._active_task.done():
                self._active_task.cancel()
                try:
                    await self._active_task
                except asyncio.CancelledError:
                    pass
            self._is_active = False





# =============================================================================
# Video Stream Manager
# =============================================================================
class VideoStreamManager:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._clients: set[asyncio.Queue] = set()
        self._stream_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def add_client(self) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=10)
        async with self._lock:
            self._clients.add(queue)
            if not self._stream_task or self._stream_task.done():
                self._stream_task = asyncio.create_task(self._stream_from_camera())
        return queue

    async def remove_client(self, queue: asyncio.Queue):
        async with self._lock:
            self._clients.discard(queue)
            if not self._clients and self._stream_task:
                self._stream_task.cancel()

    async def _stream_from_camera(self):
        url = f"http://{self._settings.AXIS_USERNAME}:{self._settings.AXIS_PASSWORD}@{self._settings.AXIS_HOSTNAME}/axis-cgi/mjpg/video.cgi"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        # Send to all connected clients
                        dead_clients = set()
                        for queue in self._clients.copy():
                            try:
                                queue.put_nowait(chunk)
                            except asyncio.QueueFull:
                                dead_clients.add(queue)
                        
                        # Remove dead clients
                        if dead_clients:
                            async with self._lock:
                                self._clients -= dead_clients
                                
        except Exception as e:
            logger.debug(f"Camera stream error: {e}")


class VideoBufferManager:
    def __init__(self, settings: Settings, active_state: ActiveState):
        self._settings = settings
        self._active_state = active_state
        self._buffer: list[bytes] = []
        self._buffer_lock = asyncio.Lock()
        self._stream_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._url = f"http://{self._settings.AXIS_HOSTNAME}/axis-cgi/mjpg/video.cgi"
        self._max_buffer_size = 50  # Keep last 50 frames

    async def startup(self):
        self._client = httpx.AsyncClient(timeout=30.0)

    async def shutdown(self):
        await self.stop_buffering()
        if self._client:
            await self._client.aclose()

    async def start_buffering(self):
        """Start buffering video stream"""
        if self._stream_task and not self._stream_task.done():
            return
        logger.info("Starting video buffering")
        self._stream_task = asyncio.create_task(self._buffer_stream())

    async def stop_buffering(self):
        """Stop buffering and clear buffer"""
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
        async with self._buffer_lock:
            self._buffer.clear()
        logger.info("Stopped video buffering")

    async def get_buffered_stream(self):
        """Return buffered frames as async generator"""
        async with self._buffer_lock:
            buffer_copy = self._buffer.copy()
        
        for frame in buffer_copy:
            yield frame

    async def _buffer_stream(self):
        """Buffer incoming video stream"""
        try:
            async with self._client.stream("GET", self._url) as response:
                response.raise_for_status()
                boundary = self._extract_boundary(response.headers.get("content-type", ""))
                
                buffer = b""
                async for chunk in response.aiter_bytes():
                    buffer += chunk
                    frames = self._parse_mjpeg_frames(buffer, boundary)
                    
                    for frame in frames:
                        async with self._buffer_lock:
                            self._buffer.append(frame)
                            if len(self._buffer) > self._max_buffer_size:
                                self._buffer.pop(0)
                    
                    # Keep only unparsed data
                    if frames:
                        buffer = buffer.split(boundary.encode())[-1]
                        
        except Exception as e:
            logger.error(f"Video buffering error: {e}")

    def _extract_boundary(self, content_type: str) -> str:
        """Extract boundary from content-type header"""
        if "boundary=" in content_type:
            return "--" + content_type.split("boundary=")[1].split(";")[0]
        return "--myboundary"

    def _parse_mjpeg_frames(self, buffer: bytes, boundary: str) -> list[bytes]:
        """Parse MJPEG frames from buffer"""
        frames = []
        parts = buffer.split(boundary.encode())
        
        for part in parts[1:-1]:  # Skip first empty and last incomplete
            if b"\r\n\r\n" in part:
                frame_data = part.split(b"\r\n\r\n", 1)[1]
                if frame_data.startswith(b"\xff\xd8"):  # JPEG header
                    frames.append(boundary.encode() + b"\r\n" + part + b"\r\n")
        
        return frames
# =============================================================================
# Video Buffer Manager
# =============================================================================
class VideoBufferManager:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._buffer = b""
        self._lock = asyncio.Lock()
        self._buffering = False

    async def start_buffering(self):
        if self._buffering:
            return
        self._buffering = True
        logger.info("Starting video buffering")
        asyncio.create_task(self._capture_stream())

    async def stop_buffering(self):
        self._buffering = False
        async with self._lock:
            self._buffer = b""
        logger.info("Stopped video buffering")

    async def get_stream(self):
        """Stream buffered content then live stream"""
        # First yield buffered content if available
        async with self._lock:
            if self._buffer:
                yield self._buffer
        
        # Always stream live regardless of buffering state
        url = f"http://{self._settings.AXIS_USERNAME}:{self._settings.AXIS_PASSWORD}@{self._settings.AXIS_HOSTNAME}/axis-cgi/mjpg/video.cgi"
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("GET", url) as response:
                async for chunk in response.aiter_bytes():
                    # Also buffer if we're in buffering mode
                    if self._buffering:
                        async with self._lock:
                            self._buffer += chunk
                            if len(self._buffer) > 1024 * 1024:  # 1MB max
                                self._buffer = self._buffer[-512 * 1024:]  # Keep last 512KB
                    yield chunk

    async def _capture_stream(self):
        url = f"http://{self._settings.AXIS_USERNAME}:{self._settings.AXIS_PASSWORD}@{self._settings.AXIS_HOSTNAME}/axis-cgi/mjpg/video.cgi"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream("GET", url) as response:
                    buffer_size = 0
                    max_buffer = 1024 * 1024  # 1MB buffer
                    
                    async for chunk in response.aiter_bytes():
                        if not self._buffering:
                            break
                            
                        async with self._lock:
                            self._buffer += chunk
                            buffer_size += len(chunk)
                            
                            # Keep buffer size manageable
                            if buffer_size > max_buffer:
                                self._buffer = self._buffer[-max_buffer//2:]
                                buffer_size = len(self._buffer)
                                
        except Exception as e:
            logger.error(f"Video capture error: {e}")


class CameraFetcher:
    def __init__(self, settings: Settings, active_state: ActiveState):
        self._settings = settings
        self._active_state = active_state
        self._lock = asyncio.Lock()
        self._image: bytes | None = None
        self._last_fetch_time: float = 0.0
        self._client: httpx.AsyncClient | None = None
        self._url = f"http://{self._settings.AXIS_USERNAME}:{self._settings.AXIS_PASSWORD}@{self._settings.AXIS_HOSTNAME}/axis-cgi/jpg/image.cgi?camera=1"
        self._fetch_task: asyncio.Task | None = None

    async def startup(self):
        logger.info("Initializing camera client")
        self._client = httpx.AsyncClient(timeout=5.0)
        self._url = f"http://{self._settings.AXIS_USERNAME}:{self._settings.AXIS_PASSWORD}@{self._settings.AXIS_HOSTNAME}/axis-cgi/jpg/image.cgi?camera=1"
        # Start background fetching
        self._fetch_task = asyncio.create_task(self._background_fetch_loop())

    async def shutdown(self):
        if self._fetch_task:
            self._fetch_task.cancel()
            try:
                await self._fetch_task
            except asyncio.CancelledError:
                pass
        if self._client:
            logger.info("Closing camera client")
            await self._client.aclose()

    async def get_image(self) -> bytes:
        """Return cached image, fetch if not available"""
        if self._image is None:
            # No cache yet, fetch immediately
            await self._fetch_image()
        return self._image

    async def _background_fetch_loop(self):
        """Background task that fetches images at different intervals"""
        while True:
            try:
                await self._fetch_image()
                # Dynamic interval: 2s when active, 60s when inactive
                interval = 2 if self._active_state.is_active else 60
                logger.debug(f"Next fetch in {interval}s (active: {self._active_state.is_active})")
                
                # Sleep in 1s chunks to respond quickly to state changes
                for _ in range(interval):
                    await asyncio.sleep(1)
                    # If state changed to active, break early and fetch immediately
                    if not self._active_state.is_active and interval == 60:
                        continue
                    if self._active_state.is_active and interval == 60:
                        break
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Background fetch error: {e}")
                await asyncio.sleep(5)  # Retry after 5s on error

    async def _fetch_image(self):
        """Fetch and cache a new image"""
        if not self._client:
            return
        
        try:
            resp = await self._client.get(self._url)
            resp.raise_for_status()
            async with self._lock:
                self._image = resp.content
                self._last_fetch_time = time.time()
            logger.debug(f"Cached new image ({len(self._image)} bytes)")
        except Exception as e:
            logger.error(f"Failed to fetch camera image: {e}")


# =============================================================================
# GPIO Manager (LED + Buttons)
# =============================================================================
class GPIOManager:
    def __init__(self, settings: Settings, event_bus: EventBus, loop: asyncio.AbstractEventLoop):
        self._settings = settings
        self._event_bus = event_bus
        self._loop = loop
        self._stop_polling = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        logger.info("Starting GPIO manager")
        self._setup_gpio()
        self._thread = threading.Thread(target=self._polling_loop, daemon=True)
        self._thread.start()
        
    def stop(self):
        logger.info("Stopping GPIO manager")
        if self._thread:
            self._stop_polling.set()
            self._thread.join(timeout=1.0)
        
        # Ensure LED is off before cleanup
        try:
            self.led_off()
            logger.info("LED turned off before shutdown")
            # Give a small delay to ensure the GPIO state is set
            import time
            time.sleep(0.1)
        except Exception as e:
            logger.warning(f"Failed to turn off LED during shutdown: {e}")
        
        GPIO.cleanup()
        logger.info("GPIO cleanup complete")

    def _setup_gpio(self):
        GPIO.setwarnings(False)
        GPIO.cleanup()  # Clean up any previous GPIO usage
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self._settings.LED_PIN, GPIO.OUT, initial=GPIO.HIGH)  # OFF
        for pin in self._settings.BUTTON_PINS:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        logger.info(f"GPIO setup complete (LED={self._settings.LED_PIN}, BUTTONS={self._settings.BUTTON_PINS})")

    def led_on(self):
        logger.info("LED ON")
        GPIO.output(self._settings.LED_PIN, GPIO.LOW)

    def led_off(self):
        logger.info("LED OFF")
        GPIO.output(self._settings.LED_PIN, GPIO.HIGH)
    
    async def led_on_async(self):
        await asyncio.to_thread(self.led_on)
        
    async def led_off_async(self):
        await asyncio.to_thread(self.led_off)

    def _polling_loop(self):
        last_press_time = [0.0] * len(self._settings.BUTTON_PINS)
        debounce_secs = self._settings.BUTTON_DEBOUNCE_MS / 1000.0
        
        while not self._stop_polling.is_set():
            current_time = time.time()
            for i, pin in enumerate(self._settings.BUTTON_PINS):
                # Button pressed is LOW (PUD_UP)
                if GPIO.input(pin) == GPIO.LOW:
                    if (current_time - last_press_time[i]) > debounce_secs:
                        last_press_time[i] = current_time
                        button_num = i + 1
                        logger.info(f"Button {button_num} pressed (Pin {pin})")
                        asyncio.run_coroutine_threadsafe(
                            self._event_bus.emit("BELL_PRESSED", button_num), self._loop
                        )
            time.sleep(0.02) # 20ms poll interval


# =============================================================================
# UDP Server (Listener + Broadcaster)
# =============================================================================
class UDPServer:
    class _UDPProtocol(asyncio.DatagramProtocol):
        def __init__(self, owner: 'UDPServer'):
            self.owner = owner
            self.transport: asyncio.DatagramTransport | None = None
            self.log = logging.getLogger("doorbell.udp")

        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            self.transport = transport  # type: ignore
            self.log.info(f"UDP listener ready on {self.transport.get_extra_info('sockname')}")

        def datagram_received(self, data: bytes, addr) -> None:
            msg = data.decode(errors="ignore").strip()
            self.log.debug(f"UDP from {addr}: {msg!r}")
            asyncio.create_task(self.owner._handle_command(msg, addr, self.transport))

    def __init__(self, settings: Settings, active_state: ActiveState, event_bus: EventBus):
        self._settings = settings
        self._active_state = active_state
        self._event_bus = event_bus
        self._transport: asyncio.DatagramTransport | None = None
        self._bcast_task: asyncio.Task | None = None

    async def start(self, loop: asyncio.AbstractEventLoop):
        if self._settings.UDP_LISTEN_PORT > 0:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: self._UDPProtocol(self),
                local_addr=("0.0.0.0", self._settings.UDP_LISTEN_PORT),
                allow_broadcast=True,
            )
        if self._settings.UDP_BROADCAST_ENABLED:
            self._bcast_task = asyncio.create_task(self._broadcast_loop())

    async def stop(self):
        logger.info("Stopping UDP services")
        if self._transport:
            self._transport.close()
        if self._bcast_task:
            self._bcast_task.cancel()
            try:
                await self._bcast_task
            except asyncio.CancelledError:
                pass

    async def _handle_command(self, msg: str, addr, transport: asyncio.DatagramTransport):
        parts = msg.split()
        cmd = (parts[0].upper() if parts else "")
        if cmd == "PING":
            transport.sendto(b"PONG", addr)
        elif cmd == "STATUS":
            payload = json.dumps({
                "active": self._active_state.is_active,
                "activated_at": self._active_state.activated_at,
                "uptime": time.time() - START_TIME,
            }).encode()
            transport.sendto(payload, addr)
        elif cmd == "BELL":
            n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            await self._event_bus.emit("BELL_PRESSED", n)
            transport.sendto(b"OK", addr)
        elif cmd == "ACTIVATE":
            secs = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            await self._active_state.activate(secs)
            transport.sendto(b"OK", addr)
        else:
            transport.sendto(b"ERR unknown command", addr)

    async def broadcast_button_press(self, button_id: int):
        """Broadcast a UDP message when a button is pressed"""
        if not self._settings.UDP_BROADCAST_ENABLED:
            return
            
        log = logging.getLogger("doorbell.udp.broadcast")
        addr = (self._settings.UDP_BCAST_ADDR, self._settings.UDP_BCAST_PORT)
        
        payload = json.dumps({
            "type": "doorbell_button_press",
            "button_id": button_id,
            "timestamp": time.time(),
            "active": self._active_state.is_active
        }).encode()
        
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(payload, addr)
                log.info(f"Broadcasted button {button_id} press to {addr[0]}:{addr[1]}")
        except Exception as e:
            log.warning(f"Button press broadcast failed: {e}")

    async def _broadcast_loop(self):
        log = logging.getLogger("doorbell.udp.broadcast")
        addr = (self._settings.UDP_BCAST_ADDR, self._settings.UDP_BCAST_PORT)
        log.info(f"Heartbeat broadcast to {addr[0]}:{addr[1]} every {self._settings.UDP_BCAST_INTERVAL}s")
        
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            while True:
                payload = json.dumps({
                    "type": "doorbell_heartbeat",
                    "active": self._active_state.is_active,
                    "activated_at": self._active_state.activated_at,
                    "uptime": time.time() - START_TIME,
                    "ts": time.time(),
                }).encode()
                try:
                    s.sendto(payload, addr)
                except Exception as e:
                    log.warning(f"Broadcast failed: {e}")
                await asyncio.sleep(self._settings.UDP_BCAST_INTERVAL)


# =============================================================================
# API Endpoints
# =============================================================================
async def get_status_endpoint(request):
    return JSONResponse({
        "active": active_state.is_active,
        "activated_at": active_state.activated_at,
        "uptime": time.time() - START_TIME,
        "sip": sip_server.get_call_info(),
        "config": {
            "buttons": settings.BUTTON_PINS,
            "led_pin": settings.LED_PIN,
            "axis_host": settings.AXIS_HOSTNAME,
            "sip_enabled": settings.SIP_ENABLED,
            "sip_port": settings.SIP_PORT,
        }
    })

async def get_lazy_image_endpoint(request):
    try:
        image_bytes = await camera_fetcher.get_image()
        return Response(image_bytes, media_type="image/jpeg")
    except Exception as e:
        logger.error(f"Image endpoint error: {e}")
        return JSONResponse({"error": "No cached image available"}, status_code=503)

async def proxy_video_endpoint(request):
    url = f"http://{settings.AXIS_USERNAME}:{settings.AXIS_PASSWORD}@{settings.AXIS_HOSTNAME}/axis-cgi/mjpg/video.cgi"
    
    async def stream_generator():
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        yield chunk
        except (httpx.StreamClosed, httpx.ConnectError, asyncio.CancelledError):
            return
        except Exception as e:
            logger.error(f"Video stream error: {e}")
            return
    
    return StreamingResponse(
        stream_generator(),
        media_type="multipart/x-mixed-replace; boundary=myboundary",
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }
    )



async def healthz_endpoint(request):
    return PlainTextResponse("ok")

routes = [
    Route("/status", get_status_endpoint),
    Route("/image", get_lazy_image_endpoint),
    Route("/video", proxy_video_endpoint),
    Route("/healthz", healthz_endpoint),
]

# =============================================================================
# App Initialization & Lifespan
# =============================================================================

# Global reference for signal handler
gpio_manager_global = None

def signal_handler(signum, frame):
    """Handle shutdown signals to ensure LED is turned off"""
    logger.info(f"Received signal {signum}, forcing immediate exit...")
    if gpio_manager_global:
        try:
            gpio_manager_global.led_off()
            logger.info("LED turned off via signal handler")
        except Exception as e:
            logger.warning(f"Failed to turn off LED in signal handler: {e}")
    print("Exiting now...")
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

event_bus = EventBus()
active_state = ActiveState(event_bus, settings.ACTIVE_TIMEOUT)
camera_fetcher = CameraFetcher(settings, active_state)
video_stream = VideoStreamManager(settings)
udp_server = UDPServer(settings, active_state, event_bus)
sip_server = SIPServer(settings, event_bus)
gpio_manager: GPIOManager | None = None

# Link active state with SIP server
 

event_bus.subscribe("BELL_PRESSED", active_state.handle_bell_pressed)
event_bus.subscribe("BELL_PRESSED", udp_server.broadcast_button_press)

@asynccontextmanager
async def lifespan(app: Starlette):
    global gpio_manager, gpio_manager_global
    loop = asyncio.get_running_loop()
    
    logger.info("Application starting...")
    gpio_manager = GPIOManager(settings, event_bus, loop)
    gpio_manager_global = gpio_manager  # Set global reference for signal handler
    event_bus.subscribe("ACTIVE_STARTED", gpio_manager.led_on_async)
    event_bus.subscribe("ACTIVE_ENDED", gpio_manager.led_off_async)
    
    await camera_fetcher.startup()
    gpio_manager.start()
    await udp_server.start(loop)
    await sip_server.start(loop)
    
    try:
        yield
    finally:
        logger.info("Application shutting down...")
        await udp_server.stop()
        await sip_server.stop()
        if gpio_manager:
            gpio_manager.stop()
        await camera_fetcher.shutdown()
        await active_state.shutdown()
        logger.info("Shutdown complete.")

app = Starlette(routes=routes, lifespan=lifespan)

# =============================================================================
# Entrypoint
# =============================================================================
if __name__ == "__main__":
    import uvicorn
    # --- MODIFICATION: Added log_config=None to prevent Uvicorn from
    # --- overriding our logging setup.
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None
    )