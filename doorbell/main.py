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

import audioop

# =============================================================================
# Credential Sanitization (MUST BE FIRST)
# =============================================================================
import logging
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
import os
import re
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, Optional
from pathlib import Path
from typing import Awaitable, Callable, List

import httpx
from starlette.applications import Starlette
from starlette.responses import (JSONResponse, PlainTextResponse, Response,
                                 StreamingResponse)
from starlette.routing import Route

# Import PySIP server
try:
    from .pysip_server import PySIPServer
    PYSIP_AVAILABLE = True
except ImportError:
    try:
        from pysip_server import PySIPServer
        PYSIP_AVAILABLE = True
    except ImportError:
        PYSIP_AVAILABLE = False
        PySIPServer = None

try:
    import pyaudio
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False

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
def get_local_ip_address() -> str:
    """Get the local IP address of the machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        s.connect(('10.255.255.255', 1))
        ip_address = s.getsockname()[0]
    except Exception:
        ip_address = socket.gethostbyname(socket.gethostname())
    finally:
        s.close()
    return ip_address

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
        
        # HTTP Configuration
        object.__setattr__(self, 'HTTP_PORT', int(os.environ.get("HTTP_PORT", config.get("http_port", "8000"))))
        
        # SIP Configuration
        object.__setattr__(self, 'SIP_PORT', int(os.environ.get("SIP_PORT", config.get("sip_port", "5060"))))
        object.__setattr__(self, 'SIP_USER', os.environ.get("SIP_USER", config.get("sip_user", "door")))
        object.__setattr__(self, 'SIP_DOMAIN', os.environ.get("SIP_DOMAIN", config.get("sip_domain", "doorbell.local")))
        object.__setattr__(self, 'SIP_ENABLED', os.environ.get("SIP_ENABLED", str(config.get("sip_enabled", True))).lower() in ("1", "true"))
        
        # TTS Configuration - removed, audio comes via SIP

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
    force=True,
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
# Audio Manager
# =============================================================================
class AudioManager:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._audio = None
        self._stream = None
        self._sample_rate = 8000  # Default, will be updated during initialization
        self._log = logging.getLogger("doorbell.audio")
        
    async def startup(self):
        if not AUDIO_AVAILABLE:
            self._log.info("PyAudio not available")
            return
            
        try:
            self._audio = pyaudio.PyAudio()
            
            # Find a suitable output device
            output_device = None
            for i in range(self._audio.get_device_count()):
                device_info = self._audio.get_device_info_by_index(i)
                if device_info['maxOutputChannels'] > 0:
                    self._log.info(f"Found audio output device: {device_info['name']}")
                    if output_device is None:  # Use first available output device
                        output_device = i
                        break
            
            # Setup audio stream for playback - try multiple sample rates
            sample_rates = [8000, 16000, 22050, 44100, 48000]  # G.711 first, then common rates
            
            for rate in sample_rates:
                try:
                    self._stream = self._audio.open(
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=rate,
                        output=True,
                        output_device_index=output_device,
                        frames_per_buffer=int(rate * 0.02)  # 20ms buffer
                    )
                    self._sample_rate = rate
                    self._log.info(f"Audio initialized at {rate}Hz")
                    break
                except Exception as e:
                    self._log.debug(f"Failed to initialize audio at {rate}Hz: {e}")
                    continue
            else:
                raise Exception("No supported sample rate found")
            self._log.info(f"Audio system initialized with device {output_device}")
        except Exception as e:
            self._log.error(f"Failed to initialize audio: {e}")
            self._audio = None
            self._stream = None
            
    async def play_audio(self, audio_data: bytes):
        """Play audio data to speaker"""
        if self._stream and self._audio:
            try:
                # Convert G.711 u-law to 16-bit PCM
                pcm_data = audioop.ulaw2lin(audio_data, 2)
                self._stream.write(pcm_data)
            except Exception as e:
                self._log.error(f"Audio playback error: {e}")
        else:
            self._log.debug("Audio stream not available for playback")
                
    async def shutdown(self):
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
        if self._audio:
            self._audio.terminate()


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
        self._sip_server: 'SIPServer | None' = None

    def set_sip_server(self, sip_server: 'SIPServer'):
        """Set reference to SIP server for call state checking"""
        self._sip_server = sip_server

    @property
    def is_active(self) -> bool: 
        # Stay active if there's an ongoing call
        if self._sip_server and self._sip_server.is_call_active:
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




# CallState class for SIP server
@dataclass
class CallState:
    state: str = "idle"  # idle, ringing, active, ended
    call_id: str = ""
    remote_addr: tuple | None = None
    start_time: float = 0.0
    answer_time: float = 0.0
    flat_id: str = ""  # Identifier for the flat
    rtp_port: int = 0
    rtp_socket: socket.socket | None = None
    rtp_task: asyncio.Task | None = None
    timeout_task: asyncio.Task | None = None
    rtp_timeout_task: asyncio.Task | None = None
    last_rtp_time: float = 0.0

class SIPServer:
    def __init__(self, settings: Settings, event_bus: EventBus, audio_manager: 'AudioManager'):
        self._settings = settings
        self._event_bus = event_bus
        self._audio_manager = audio_manager
        self._transport: asyncio.DatagramTransport | None = None
        # Multi-call support
        self._active_calls: Dict[str, CallState] = {}
        self._flat_calls: Dict[str, str] = {}
        # Configuration for flats
        self._allowed_flats = {
            "flat1": {"user": "flat1", "display_name": "Flat 1", "ip_pattern": ".101"},
            "flat2": {"user": "flat2", "display_name": "Flat 2", "ip_pattern": ".102"},
            "flat3": {"user": "flat3", "display_name": "Flat 3", "ip_pattern": ".103"},
            "loxone": {"user": "smarthome", "display_name": "Loxone System", "ip_pattern": ".213"}
        }
        self._auto_answer_task: asyncio.Task | None = None
        self._call_timeout_task: asyncio.Task | None = None
        self._rtp_socket: socket.socket | None = None
        self._rtp_task: asyncio.Task | None = None
        self._log = logging.getLogger("doorbell.sip")

    @property
    def call_state(self) -> str:
        # Return state of first active call, or "idle" if none
        for call in self._active_calls.values():
            if call.state != "idle":
                return call.state
        return "idle"

    def _identify_flat(self, msg: dict, addr: tuple) -> str:
        """Identify which flat is calling based on SIP headers and IP"""
        # Try to extract flat ID from From header
        from_header = msg['headers'].get('from', '').lower()
        
        # Look for flat identifiers in the From header
        for flat_id, config in self._allowed_flats.items():
            if config['user'].lower() in from_header:
                self._log.info(f"Identified {flat_id} by SIP user: {config['user']}")
                return flat_id
                
        # Fallback: use IP address to identify flat
        ip = addr[0]
        for flat_id, config in self._allowed_flats.items():
            if config['ip_pattern'] in ip:
                self._log.info(f"Identified {flat_id} by IP pattern: {config['ip_pattern']} in {ip}")
                return flat_id
        
        # Default fallback
        fallback_id = f"unknown_{ip.replace('.', '_')}"
        self._log.warning(f"Could not identify flat, using fallback: {fallback_id}")
        return fallback_id

    @property
    def is_call_active(self) -> bool:
        """Check if any call is active"""
        return self._settings.SIP_ENABLED and any(
            call.state in ("ringing", "active") for call in self._active_calls.values()
        )

    async def start(self, loop: asyncio.AbstractEventLoop):
        if not self._settings.SIP_ENABLED:
            self._log.info("SIP server disabled")
            return
        
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: self._SIPProtocol(self),
                local_addr=("0.0.0.0", self._settings.SIP_PORT)
            )
            self._log.info(f"SIP server listening on port {self._settings.SIP_PORT}")
        except Exception as e:
            self._log.error(f"Failed to start SIP server: {e}")
            # Don't fail the entire app if SIP fails
            pass

    async def stop(self):
        # End all active calls
        for call_id in list(self._active_calls.keys()):
            await self._end_call(call_id, "shutdown")
        
        if self._transport:
            self._transport.close()
            self._transport = None
            self._log.info("SIP server stopped")

    def _parse_sip_message(self, data: bytes) -> dict:
        """Parse basic SIP message"""
        lines = data.decode().strip().split('\r\n')
        if not lines:
            return {}
            
        # Parse request line
        request_line = lines[0].split()
        if len(request_line) < 3:
            return {}
            
        headers = {}
        for line in lines[1:]:
            if ':' in line:
                key, value = line.split(':', 1)
                headers[key.strip().lower()] = value.strip()
                
        return {
            'method': request_line[0],
            'uri': request_line[1],
            'version': request_line[2],
            'headers': headers
        }

    def _create_response(self, code: int, reason: str, call_id: str, headers: dict = None) -> bytes:
        """Create SIP response"""
        response = f"SIP/2.0 {code} {reason}\r\n"
        response += f"Call-ID: {call_id}\r\n"
        response += f"Via: {headers.get('via', '')}\r\n"
        response += f"From: {headers.get('from', '')}\r\n"
        response += f"To: {headers.get('to', '')}\r\n"
        response += f"CSeq: {headers.get('cseq', '')}\r\n"
        response += "Content-Length: 0\r\n\r\n"
        return response.encode()

    def _send_sip_message(self, message: bytes, addr: tuple, description: str = ""):
        """Send SIP message and log it"""
        if self._transport:
            try:
                # Enhanced logging with timestamp and size
                import time
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                
                self._log.info(f"=== OUTGOING SIP MESSAGE TO {addr} {description} ===")
                self._log.info(f"Timestamp: {timestamp}")
                self._log.info(f"Size: {len(message)} bytes")
                
                # Ensure message is properly handled for logging
                if isinstance(message, bytes):
                    log_message = message.decode('utf-8', errors='ignore')
                else:
                    log_message = str(message)
                self._log.info(log_message)
                self._log.info("=== END OUTGOING SIP MESSAGE ===")
                
                # Also log to a separate SIP-only file if needed
                self._log_sip_message("OUTGOING", addr, log_message, description)
                
                self._transport.sendto(message, addr)
            except Exception as e:
                self._log.error(f"Failed to send SIP message to {addr}: {e}")
                # Add traceback for debugging
                import traceback
                self._log.debug(f"SIP send error details: {traceback.format_exc()}")

    def _log_sip_message(self, direction: str, addr: tuple, message: str, description: str = ""):
        """Log SIP message to separate detailed log"""
        import time
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        # You can uncomment this to write to a separate SIP log file
        # try:
        #     with open("/tmp/doorbell_sip.log", "a") as f:
        #         f.write(f"\n[{timestamp}] {direction} {addr} {description}\n")
        #         f.write(f"{message}\n")
        #         f.write("-" * 80 + "\n")
        # except:
        #     pass
        
        # For now, just add extra detail to main log
        self._log.debug(f"SIP {direction} {addr}: {len(message)} bytes {description}")

    async def _handle_invite(self, msg: dict, addr: tuple):
        """Handle incoming INVITE with multi-flat support"""
        call_id = msg['headers'].get('call-id', '')
        flat_id = self._identify_flat(msg, addr)
        
        self._log.info(f"INVITE from {flat_id} ({addr}) - Call ID: {call_id}")
        
        # Create new call state
        call_state = CallState(
            call_id=call_id,
            state="ringing",
            remote_addr=addr,
            start_time=time.time(),
            flat_id=flat_id
        )
        
        self._active_calls[call_id] = call_state
        self._flat_calls[flat_id] = call_id
        
        # Send 100 Trying
        response = self._create_response(100, "Trying", call_id, msg['headers'])
        self._send_sip_message(response, addr, f"(100 Trying - {flat_id})")
        
        # Send 180 Ringing
        response = self._create_response(180, "Ringing", call_id, msg['headers'])
        self._send_sip_message(response, addr, f"(180 Ringing - {flat_id})")
        
        self._log.info(f"Incoming call from {flat_id} ({addr})")
        await self._event_bus.emit("SIP_CALL_INCOMING", {"flat_id": flat_id, "addr": addr})
        
        # Auto-answer after 3 seconds
        asyncio.create_task(self._auto_answer_call(call_id, addr, msg['headers']))
        
        # Set call timeout (60 seconds total)
        call_state.timeout_task = asyncio.create_task(self._call_timeout(call_id))

    async def _auto_answer_call(self, call_id: str, addr: tuple, headers: dict):
        """Auto-answer call after 3 seconds"""
        try:
            await asyncio.sleep(3)
            
            # Check if call is still ringing
            if call_id in self._active_calls and self._active_calls[call_id].state == "ringing":
                await self._answer_call(call_id, addr, headers)
        except asyncio.CancelledError:
            pass

    async def _answer_call(self, call_id: str, addr: tuple, headers: dict):
        """Answer a specific call"""
        if call_id not in self._active_calls:
            return
            
        call_state = self._active_calls[call_id]
        call_state.state = "active"
        call_state.answer_time = time.time()
        call_state.last_rtp_time = time.time()
        call_state.last_rtp_time = time.time()
        
        # Setup RTP for this call
        rtp_port = await self._setup_rtp_for_call(call_id)
        call_state.rtp_port = rtp_port
        
        # Create SDP for audio
        local_ip = get_local_ip_address()
        sdp = f"""v=0
o=doorbell 123456 654321 IN IP4 {local_ip}
s=Doorbell Session
c=IN IP4 {local_ip}
t=0 0
m=audio {rtp_port} RTP/AVP 0
a=rtpmap:0 PCMU/8000"""
        
        response = f"SIP/2.0 200 OK\r\n"
        response += f"Call-ID: {call_id}\r\n"
        response += f"Via: {headers.get('via', '')}\r\n"
        response += f"From: {headers.get('from', '')}\r\n"
        response += f"To: {headers.get('to', '')};tag=doorbell-tag\r\n"
        response += f"CSeq: {headers.get('cseq', '1 INVITE')}\r\n"
        response += "Contact: <sip:door@doorbell.local>\r\n"
        response += "Content-Type: application/sdp\r\n"
        response += f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"
        
        self._send_sip_message(response.encode(), addr, f"(200 OK - {call_state.flat_id})")
        
        self._log.info(f"Call answered from {call_state.flat_id} ({addr})")
        await self._event_bus.emit("SIP_CALL_ANSWERED", {
            "flat_id": call_state.flat_id, 
            "addr": addr,
            "call_id": call_id
        })
        await self._event_bus.emit("SIP_CALL_ANSWERED", addr)

        # Start RTP timeout monitor
        call_state.rtp_timeout_task = asyncio.create_task(self._rtp_timeout_monitor(call_id))

    async def _setup_rtp_for_call(self, call_id: str) -> int:
        """Setup RTP socket for a specific call"""
        try:
            if call_id not in self._active_calls:
                return 0
                
            call_state = self._active_calls[call_id]
            
            # Create RTP socket
            rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rtp_socket.bind(('0.0.0.0', 0))  # Let system assign port
            rtp_port = rtp_socket.getsockname()[1]
            rtp_socket.setblocking(False)
                        
            call_state.rtp_socket = rtp_socket
            
            # Start RTP receiver task for this call
            task = asyncio.create_task(self._rtp_receiver_for_call(call_id))
            call_state.rtp_task = task
            
            self._log.info(f"RTP setup for {call_state.flat_id} (call {call_id}) on port {rtp_port}")
            return rtp_port
            
        except Exception as e:
            self._log.error(f"Failed to setup RTP for call {call_id}: {e}")
            return 0

    async def _rtp_receiver_for_call(self, call_id: str):
        """Receive RTP audio for a specific call"""
        if call_id not in self._active_calls:
            return
            
        call_state = self._active_calls[call_id]
        rtp_socket = call_state.rtp_socket
        
        if not rtp_socket:
            return
            
        try:
            loop = asyncio.get_event_loop()
            
            while call_id in self._active_calls and self._active_calls[call_id].state == "active":
                try:
                    try:
                        data, addr = await asyncio.wait_for(loop.sock_recvfrom(rtp_socket, 1024), timeout=1.0)
                        if len(data) > 12:
                            call_state.last_rtp_time = time.time()  # update on every packet
                            audio_payload = data[12:]
                            await self._audio_manager.play_audio(audio_payload)
                            self._log.info(f"Audio from {call_state.flat_id}: {len(audio_payload)} bytes")
                    except asyncio.TimeoutError:
                        # No data this second, let loop continue so timeout monitor can act
                        continue
                    # try:
                    #     data, addr = await asyncio.wait_for(loop.sock_recvfrom(rtp_socket, 1024), timeout=1.0)
                    #     # Simple RTP parsing (skip 12-byte header)
                    #     if len(data) > 12:
                    #         call_state.last_rtp_time = time.time()
                    #         audio_payload = data[12:]
                    #         # Play audio from this flat
                    #         await self._audio_manager.play_audio(audio_payload)

                    #         # Log which flat is speaking
                    #         self._log.info(f"Audio from {call_state.flat_id}: {len(audio_payload)} bytes")
                    # except asyncio.TimeoutError:
                    #     continue # Go to the next iteration of the while loop
                except Exception as e:
                    if call_id in self._active_calls:  # Only log if call still exists
                        self._log.error(f"RTP receive error for {call_state.flat_id}: {e}")
                    break
                    
        except Exception as e:
            self._log.error(f"RTP receiver error for call {call_id}: {e}")
        finally:
            # Cleanup
            if rtp_socket:
                try:
                    rtp_socket.close()
                except:
                    pass

    async def _rtp_timeout_monitor(self, call_id: str):
        """Monitor for RTP timeout (no audio for 3 seconds)"""
        self._log.info(f"Starting RTP timeout monitor for call {call_id}")
        try:
            while call_id in self._active_calls and self._active_calls[call_id].state == "active":
                await asyncio.sleep(1) # Check every second
                call_state = self._active_calls.get(call_id)
                if not call_state:
                    break

                time_since_last_rtp = time.time() - call_state.last_rtp_time
                self._log.debug(f"RTP timeout check for call {call_id}: {time_since_last_rtp:.2f}s since last RTP")

                if time_since_last_rtp >= 3:
                    self._log.info(f"No RTP received for 3 seconds on call {call_id}. Hanging up.")
                    await self._end_call(call_id, "rtp_timeout")
                    break
        except asyncio.CancelledError:
            self._log.info(f"RTP timeout monitor for call {call_id} cancelled.")

    async def _setup_rtp(self):
        """Setup RTP socket for audio"""
        try:
            self._rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._rtp_socket.bind(('0.0.0.0', 0))
            self._rtp_port = self._rtp_socket.getsockname()[1]
            
            # Start RTP receiver task
            self._rtp_task = asyncio.create_task(self._rtp_receiver())
            self._log.info(f"RTP listening on port {self._rtp_port}")
        except Exception as e:
            self._log.error(f"RTP setup failed: {e}")

    async def _rtp_receiver(self):
        """Receive and play RTP audio packets"""
        try:
            loop = asyncio.get_event_loop()
            while self._call.state == "active":
                # Receive RTP packet
                data, addr = await loop.sock_recvfrom(self._rtp_socket, 1024)
                
                # Simple RTP parsing (skip 12-byte header)
                if len(data) > 12:
                    audio_payload = data[12:]
                    # Play audio (G.711 PCMU format)
                    await self._audio_manager.play_audio(audio_payload)
                    
        except Exception as e:
            self._log.error(f"RTP receiver error: {e}")
        finally:
            if self._rtp_socket:
                self._rtp_socket.close()
                self._rtp_socket = None

    async def _handle_bye(self, msg: dict, addr: tuple):
        """Handle call termination"""
        call_id = msg['headers'].get('call-id', '')
        
        if call_id in self._active_calls:
            call_state = self._active_calls[call_id]
            response = self._create_response(200, "OK", call_id, msg['headers'])
            self._send_sip_message(response, addr, f"(200 OK for BYE - {call_state.flat_id})")
            
            await self._end_call(call_id, "remote_hangup")
            self._log.info(f"Call ended by {call_state.flat_id} ({addr})")
        else:
            # Send 481 Call/Transaction Does Not Exist
            response = self._create_response(481, "Call/Transaction Does Not Exist", call_id, msg['headers'])
            self._send_sip_message(response, addr, "(481 Call Does Not Exist)")
            self._log.warning(f"Received BYE for unknown call {call_id} from {addr}")

    async def _call_timeout(self, call_id: str):
        """Handle call timeout (60 seconds)"""
        try:
            await asyncio.sleep(60)
            if call_id in self._active_calls and self._active_calls[call_id].state in ("ringing", "active"):
                self._log.info(f"Call {call_id} timed out")
                await self._end_call(call_id, "timeout")
        except asyncio.CancelledError:
            pass

    async def _end_call(self, call_id: str, reason: str = "normal"):
        """End a specific call"""
        self._log.info(f"Attempting to end call {call_id} for reason: {reason}")
        if call_id not in self._active_calls:
            self._log.debug(f"Call {call_id} not found, nothing to end")
            return
            
        call_state = self._active_calls[call_id]
        flat_id = call_state.flat_id
        remote_addr = call_state.remote_addr
        
        self._log.info(f"Ending call from {flat_id} ({call_id}) - reason: {reason}")
        
        try:
            # Send BYE if call was active
            if call_state.state == "active" and remote_addr:
                try:
                    bye_msg = f"BYE sip:{self._settings.SIP_USER}@{self._settings.SIP_DOMAIN} SIP/2.0\r\n"
                    bye_msg += f"Via: SIP/2.0/UDP {socket.gethostbyname(socket.gethostname())}:{self._settings.SIP_PORT};branch=z9hG4bK-doorbell-bye\r\n"
                    bye_msg += f"From: <sip:{self._settings.SIP_USER}@{self._settings.SIP_DOMAIN}>;tag=doorbell-tag\r\n"
                    bye_msg += f"To: <sip:{flat_id}@{remote_addr[0]}>\r\n"
                    bye_msg += f"Call-ID: {call_id}\r\n"
                    bye_msg += f"CSeq: 1 BYE\r\n"
                    bye_msg += "Content-Length: 0\r\n\r\n"
                    
                    self._send_sip_message(bye_msg.encode(), remote_addr, f"(BYE - {reason})")
                    self._log.info(f"Sent BYE to {flat_id} ({remote_addr}) - reason: {reason}")
                except Exception as e:
                    self._log.error(f"Failed to send BYE to {flat_id}: {e}")
            
            # Cleanup RTP
            if call_state.rtp_task:
                call_state.rtp_task.cancel()
            if call_state.timeout_task:
                call_state.timeout_task.cancel()
            if call_state.rtp_timeout_task:
                call_state.rtp_timeout_task.cancel()
            if call_state.rtp_socket:
                try:
                    call_state.rtp_socket.close()
                except:
                    pass
        finally:
            # Ensure call state is always cleaned up
            if call_id in self._active_calls:
                del self._active_calls[call_id]
            if flat_id in self._flat_calls and self._flat_calls[flat_id] == call_id:
                del self._flat_calls[flat_id]
            
            await self._event_bus.emit("SIP_CALL_ENDED", {
                "reason": reason, 
                "flat_id": flat_id,
                "addr": remote_addr, 
                "call_id": call_id
            })
            
            self._log.info(f"Call ended: {flat_id} ({call_id}) - {reason}")

    
    async def hangup_call(self):
        """Hang up all active calls"""
        self._log.info(f"Hangup requested - {len(self._active_calls)} active calls")
        if self._active_calls:
            call_ids = list(self._active_calls.keys())
            for call_id in call_ids:
                await self._end_call(call_id, "manual_hangup")
            self._log.info("All calls manually hung up")
        else:
            self._log.info("No active calls to hang up")

    async def hangup_flat_call(self, flat_id: str):
        """Hang up call from specific flat"""
        if flat_id in self._flat_calls:
            call_id = self._flat_calls[flat_id]
            await self._end_call(call_id, "manual_hangup")
            self._log.info(f"Call from {flat_id} manually hung up")
        else:
            self._log.info(f"No active call from {flat_id}")

    def get_call_info(self) -> dict:
        """Get information about all active calls"""
        if not self._settings.SIP_ENABLED:
            return {"enabled": False}
            
        calls_info = {}
        for call_id, call_state in self._active_calls.items():
            calls_info[call_id] = {
                "flat_id": call_state.flat_id,
                "state": call_state.state,
                "remote_addr": call_state.remote_addr,
                "duration": time.time() - call_state.answer_time if call_state.answer_time > 0 else 0,
                "rtp_port": call_state.rtp_port
            }
            
        return {
            "enabled": True,
            "active_calls": len(self._active_calls),
            "calls": calls_info,
            "flats": list(self._allowed_flats.keys()),
            "any_active": self.is_call_active
        }

    async def _handle_ack(self, msg: dict, addr: tuple):
        """Handle ACK message - call is established"""
        self._log.info(f"ACK received from {addr} - call established")
        # ACK confirms the 200 OK response, call is now fully established

    async def _handle_cancel(self, msg: dict, addr: tuple):
        """Handle CANCEL message - caller wants to cancel the call"""
        call_id = msg['headers'].get('call-id', '')
        self._log.info(f"CANCEL received from {addr} for call {call_id}")

        if call_id in self._active_calls and self._active_calls[call_id].state == "ringing":
            # Send 200 OK to CANCEL
            response = self._create_response(200, "OK", call_id, msg['headers'])
            self._send_sip_message(response, addr, "(200 OK for CANCEL)")

            # Send 487 Request Terminated to original INVITE
            # (This would require tracking the original INVITE, simplified here)

            await self._end_call(call_id, "cancelled")
        else:
            # Send 481 Call/Transaction Does Not Exist
            response = self._create_response(481, "Call/Transaction Does Not Exist", call_id, msg['headers'])
            self._send_sip_message(response, addr, "(481 Call Does Not Exist)")

    async def _handle_register(self, msg: dict, addr: tuple):
        """Handle REGISTER message - client registration"""
        call_id = msg['headers'].get('call-id', '')
        self._log.info(f"REGISTER received from {addr}")
        
        # Simple registration acceptance (no authentication)
        response = self._create_response(200, "OK", call_id, msg['headers'])
        self._send_sip_message(response, addr, "(200 OK for REGISTER)")

    async def _handle_options(self, msg: dict, addr: tuple):
        """Handle OPTIONS message - capability query"""
        call_id = msg['headers'].get('call-id', '')
        self._log.info(f"OPTIONS received from {addr}")
        
        # Respond with supported methods
        response = f"SIP/2.0 200 OK\r\n"
        response += f"Call-ID: {call_id}\r\n"
        response += f"Via: {msg['headers'].get('via', '')}\r\n"
        response += f"From: {msg['headers'].get('from', '')}\r\n"
        response += f"To: {msg['headers'].get('to', '')}\r\n"
        response += f"CSeq: {msg['headers'].get('cseq', '')}\r\n"
        response += "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, INFO\r\n"
        response += "Content-Length: 0\r\n\r\n"
        self._send_sip_message(response.encode(), addr, "(200 OK for OPTIONS)")

    async def _handle_info(self, msg: dict, addr: tuple):
        """Handle INFO message - mid-call information"""
        call_id = msg['headers'].get('call-id', '')
        self._log.info(f"INFO received from {addr} for call {call_id}")

        if call_id in self._active_calls:
            response = self._create_response(200, "OK", call_id, msg['headers'])
            self._send_sip_message(response, addr, "(200 OK for INFO)")
        else:
            response = self._create_response(481, "Call/Transaction Does Not Exist", call_id, msg['headers'])
            self._send_sip_message(response, addr, "(481 Call Does Not Exist)")

    async def _handle_prack(self, msg: dict, addr: tuple):
        """Handle PRACK message - provisional response acknowledgment"""
        call_id = msg['headers'].get('call-id', '')
        self._log.info(f"PRACK received from {addr} for call {call_id}")

        response = self._create_response(200, "OK", call_id, msg['headers'])
        self._send_sip_message(response, addr, "(200 OK for PRACK)")

    async def _handle_update(self, msg: dict, addr: tuple):
        """Handle UPDATE message - session parameter update"""
        call_id = msg['headers'].get('call-id', '')
        self._log.info(f"UPDATE received from {addr} for call {call_id}")

        if call_id in self._active_calls:
            response = self._create_response(200, "OK", call_id, msg['headers'])
            self._send_sip_message(response, addr, "(200 OK for UPDATE)")
        else:
            response = self._create_response(481, "Call/Transaction Does Not Exist", call_id, msg['headers'])
            self._send_sip_message(response, addr, "(481 Call Does Not Exist)")

    async def _handle_refer(self, msg: dict, addr: tuple):
        """Handle REFER message - call transfer request"""
        call_id = msg['headers'].get('call-id', '')
        self._log.info(f"REFER received from {addr} for call {call_id}")
        
        # We don't support call transfer, send 501 Not Implemented
        response = self._create_response(501, "Not Implemented", call_id, msg['headers'])
        self._send_sip_message(response, addr, "(501 Not Implemented for REFER)")

    async def _handle_notify(self, msg: dict, addr: tuple):
        """Handle NOTIFY message - event notification"""
        call_id = msg['headers'].get('call-id', '')
        self._log.info(f"NOTIFY received from {addr} for call {call_id}")
        
        response = self._create_response(200, "OK", call_id, msg['headers'])
        self._send_sip_message(response, addr, "(200 OK for NOTIFY)")

    async def _handle_subscribe(self, msg: dict, addr: tuple):
        """Handle SUBSCRIBE message - event subscription"""
        call_id = msg['headers'].get('call-id', '')
        self._log.info(f"SUBSCRIBE received from {addr} for call {call_id}")
        
        # We don't support subscriptions, send 501 Not Implemented
        response = self._create_response(501, "Not Implemented", call_id, msg['headers'])
        self._send_sip_message(response, addr, "(501 Not Implemented for SUBSCRIBE)")

    class _SIPProtocol(asyncio.DatagramProtocol):
        def __init__(self, server: 'SIPServer'):
            self.server = server

        def connection_made(self, transport: asyncio.BaseTransport):
            self.transport = transport

        def datagram_received(self, data: bytes, addr: tuple):
            asyncio.create_task(self.server._handle_message(data, addr))

    async def _handle_message(self, data: bytes, addr: tuple):
        """Handle incoming SIP message"""
        try:
            # Enhanced logging for all incoming SIP messages
            import time
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            raw_message = data.decode('utf-8', errors='ignore')
            
            self._log.info(f"=== INCOMING SIP MESSAGE FROM {addr} ===")
            self._log.info(f"Timestamp: {timestamp}")
            self._log.info(f"Size: {len(data)} bytes")
            self._log.info(raw_message)
            self._log.info("=== END SIP MESSAGE ===")
            
            # Log to separate SIP log
            self._log_sip_message("INCOMING", addr, raw_message)
            
            msg = self._parse_sip_message(data)
            if not msg:
                self._log.warning(f"Failed to parse SIP message from {addr}")
                return
                
            method = msg.get('method', '')
            self._log.info(f"SIP {method} from {addr}")
            
            # Handle all SIP methods
            if method == "INVITE":
                await self._handle_invite(msg, addr)
            elif method == "BYE":
                await self._handle_bye(msg, addr)
            elif method == "ACK":
                await self._handle_ack(msg, addr)
            elif method == "CANCEL":
                await self._handle_cancel(msg, addr)
            elif method == "REGISTER":
                await self._handle_register(msg, addr)
            elif method == "OPTIONS":
                await self._handle_options(msg, addr)
            elif method == "INFO":
                await self._handle_info(msg, addr)
            elif method == "PRACK":
                await self._handle_prack(msg, addr)
            elif method == "UPDATE":
                await self._handle_update(msg, addr)
            elif method == "REFER":
                await self._handle_refer(msg, addr)
            elif method == "NOTIFY":
                await self._handle_notify(msg, addr)
            elif method == "SUBSCRIBE":
                await self._handle_subscribe(msg, addr)
            else:
                self._log.warning(f"Unhandled SIP method: {method} from {addr}")
                # Send 501 Not Implemented for unknown methods
                if msg['headers'].get('call-id'):
                    response = self._create_response(501, "Not Implemented", 
                                                   msg['headers']['call-id'], msg['headers'])
                    self._send_sip_message(response, addr, f"(501 Not Implemented for {method})")
                
        except Exception as e:
            self._log.error(f"SIP message error: {e}")
            import traceback
            self._log.error(f"Traceback: {traceback.format_exc()}")


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
        self.led_off() # Ensure LED is off on shutdown
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
        self._bcast_socket: socket.socket | None = None
        
        # Subscribe to button press events
        self._event_bus.subscribe("BELL_PRESSED", self._handle_bell_pressed)

    async def start(self, loop: asyncio.AbstractEventLoop):
        if self._settings.UDP_LISTEN_PORT > 0:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: self._UDPProtocol(self),
                local_addr=("0.0.0.0", self._settings.UDP_LISTEN_PORT),
                allow_broadcast=True,
            )
        
        # Create broadcast socket for immediate sending (button presses)
        if self._settings.UDP_BROADCAST_ENABLED:
            self._bcast_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self._bcast_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._bcast_task = asyncio.create_task(self._broadcast_loop())

    async def stop(self):
        logger.info("Stopping UDP services")
        if self._transport:
            self._transport.close()
        if self._bcast_socket:
            self._bcast_socket.close()
            self._bcast_socket = None
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

    async def _handle_bell_pressed(self, button_number: int):
        """Handle BELL_PRESSED event by broadcasting UDP message"""
        if not self._settings.UDP_BROADCAST_ENABLED or not self._bcast_socket:
            return
            
        log = logging.getLogger("doorbell.udp.broadcast")
        addr = (self._settings.UDP_BCAST_ADDR, self._settings.UDP_BCAST_PORT)
        
        payload = json.dumps({
            "type": "doorbell_button_pressed",
            "button": button_number,
            "timestamp": time.time(),
            "active": self._active_state.is_active,
        }).encode()
        
        try:
            self._bcast_socket.sendto(payload, addr)
            log.info(f"Broadcasted button {button_number} press to {addr[0]}:{addr[1]}")
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

async def sip_hangup_endpoint(request):
    logger.info("=== SIP HANGUP ENDPOINT CALLED ===")
    logger.info(f"Request method: {request.method}")
    logger.info(f"Request headers: {dict(request.headers)}")
    
    try:
        call_info_before = sip_server.get_call_info()
        logger.info(f"Call info before hangup: {call_info_before}")
        
        await sip_server.hangup_call()
        
        call_info_after = sip_server.get_call_info()
        logger.info(f"Call info after hangup: {call_info_after}")
        
        logger.info("=== SIP HANGUP ENDPOINT COMPLETED SUCCESSFULLY ===")
        return JSONResponse({
            "status": "call ended", 
            "before": call_info_before,
            "after": call_info_after
        })
    except Exception as e:
        logger.error(f"SIP hangup endpoint error: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

async def sip_hangup_flat_endpoint(request):
    """Hang up call from specific flat"""
    flat_id = request.path_params.get("flat_id")
    logger.info(f"=== SIP HANGUP FLAT ENDPOINT CALLED FOR {flat_id} ===")
    
    try:
        call_info_before = sip_server.get_call_info()
        logger.info(f"Call info before hangup: {call_info_before}")
        
        if hasattr(sip_server, 'hangup_flat_call'):
            await sip_server.hangup_flat_call(flat_id)
        else:
            # Fallback for older SIP server
            await sip_server.hangup_call()
        
        call_info_after = sip_server.get_call_info()
        logger.info(f"Call info after hangup: {call_info_after}")
        
        return JSONResponse({
            "status": f"call from {flat_id} ended", 
            "flat_id": flat_id,
            "before": call_info_before,
            "after": call_info_after
        })
    except Exception as e:
        logger.error(f"SIP hangup flat endpoint error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

async def healthz_endpoint(request):
    return PlainTextResponse("ok")

routes = [
    Route("/status", get_status_endpoint),
    Route("/image", get_lazy_image_endpoint),
    Route("/video", proxy_video_endpoint),
    Route("/sip/hangup", sip_hangup_endpoint, methods=["POST"]),
    Route("/sip/hangup/{flat_id}", sip_hangup_flat_endpoint, methods=["POST"]),
    Route("/healthz", healthz_endpoint),
]

# =============================================================================
# App Initialization & Lifespan
# =============================================================================
event_bus = EventBus()
active_state = ActiveState(event_bus, settings.ACTIVE_TIMEOUT)
camera_fetcher = CameraFetcher(settings, active_state)
video_stream = VideoStreamManager(settings)
udp_server = UDPServer(settings, active_state, event_bus)
audio_manager = AudioManager(settings)
if PYSIP_AVAILABLE:
    sip_server = PySIPServer(settings, event_bus)
else:
    sip_server = SIPServer(settings, event_bus, audio_manager)
gpio_manager: GPIOManager | None = None

# Link active state with SIP server
active_state.set_sip_server(sip_server) 

event_bus.subscribe("BELL_PRESSED", active_state.handle_bell_pressed)

@asynccontextmanager
async def lifespan(app: Starlette):
    global gpio_manager
    loop = asyncio.get_running_loop()
    
    # Register signal handlers - simplified approach
    import signal
    import os
    
    def immediate_exit_handler(signum, frame):
        print(f"\nReceived signal {signum}, forcing immediate exit...")
        try:
            # Try to hang up any active call quickly
            if sip_server.is_call_active:
                print("Hanging up active call...")
        except:
            pass
        print("Exiting now...")
        os._exit(0)
    
    # Use the standard signal.signal() instead of loop.add_signal_handler()
    signal.signal(signal.SIGINT, immediate_exit_handler)
    signal.signal(signal.SIGTERM, immediate_exit_handler)
    
    logger.info("Application starting...")
    gpio_manager = GPIOManager(settings, event_bus, loop)
    event_bus.subscribe("ACTIVE_STARTED", gpio_manager.led_on_async)
    event_bus.subscribe("ACTIVE_ENDED", gpio_manager.led_off_async)
    
    await camera_fetcher.startup()
    gpio_manager.start()
    await udp_server.start(loop)
    await audio_manager.startup()
    await sip_server.start(loop)
    
    try:
        yield
    finally:
        logger.info("Application shutting down...")
        # Perform graceful shutdown
        if sip_server:
            await sip_server.stop()
        if audio_manager:
            await audio_manager.shutdown()
        if udp_server:
            await udp_server.stop()
        if gpio_manager:
            gpio_manager.stop()
        if camera_fetcher:
            await camera_fetcher.shutdown()
        logger.info("Cleanup complete.")

app = Starlette(routes=routes, lifespan=lifespan)

# =============================================================================
# Entrypoint
# =============================================================================
def main():
    """Entry point for the doorbell application"""
    import os
    # Test file write to diagnose output issues
    try:
        with open("/tmp/doorbell_test_write.log", "a") as f:
            f.write(f"[{os.getpid()}] Doorbell application started successfully at {time.time()}\n")
    except Exception as e:
        print(f"ERROR: Could not write to /tmp/doorbell_test_write.log: {e}", file=sys.stderr)


    import uvicorn
    import signal
    import sys
    
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        sys.exit(0)
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # --- MODIFICATION: Added log_config=None to prevent Uvicorn from
        # --- overriding our logging setup.
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=settings.HTTP_PORT,
            log_config=None
        )
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down...")
    except Exception as e:
        logger.error(f"Application error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()