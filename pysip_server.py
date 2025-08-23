
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer, MediaRelay
from pysip import PySIP, Call as PySIPCall, Account, PySIPServer
from pysip.actions import Answer, Hangup

logger = logging.getLogger(__name__)

@dataclass
class Call:
    call: PySIPCall
    pc: RTCPeerConnection = field(default_factory=RTCPeerConnection)
    player: MediaPlayer = field(default=None)
    last_rtp_activity: float = field(default_factory=time.time)
    last_bytes_received: int = 0

class SIPServer:
    def __init__(self, settings, event_bus):
        self._settings = settings
        self._event_bus = event_bus
        self._loop = asyncio.get_event_loop()
        self._pysip_server = PySIPServer(
            ip_addr="0.0.0.0",
            port=self._settings.SIP_PORT,
            transport="udp",
            handler=self._handle_call,
            loop=self._loop
        )
        self._calls: Dict[str, Call] = {}
        self._relay = MediaRelay()

    async def start(self):
        if not self._settings.SIP_ENABLED:
            logger.info("SIP server disabled")
            return
        await self._pysip_server.start()
        logger.info(f"SIP server listening on port {self._settings.SIP_PORT}")
        self._loop.create_task(self._check_rtp_activity())

    async def stop(self):
        if self._pysip_server.is_running():
            await self._pysip_server.stop()
        for call_id in list(self._calls.keys()):
            await self._hangup(call_id)
        logger.info("SIP server stopped")

    @property
    def is_call_active(self) -> bool:
        return len(self._calls) > 0

    def get_call_info(self) -> dict:
        if not self._settings.SIP_ENABLED:
            return {"enabled": False}

        return {
            "enabled": True,
            "state": "active" if self.is_call_active else "idle",
            "active": self.is_call_active,
            "duration": 0  # This would need more logic to track per-call duration
        }

    async def _handle_call(self, call: PySIPCall):
        call_id = call.id
        if call_id in self._calls:
            logger.warning(f"Call with id {call_id} already exists.")
            return

        new_call = Call(call=call)
        self._calls[call_id] = new_call

        @call.on_state
        async def on_state_change(new_state):
            logger.info(f"Call {call_id} state changed to {new_state}")
            if new_state == "disconnected":
                await self._hangup(call_id)

        @call.on_media
        async def on_media(media_event):
            if media_event.type == "audio" and media_event.active:
                logger.info(f"Answering call {call_id}")
                await self._answer(call_id, media_event.sdp)

        await self._event_bus.emit("SIP_CALL_INCOMING", call.remote_uri)

    async def _answer(self, call_id: str, remote_sdp: str):
        if call_id not in self._calls:
            return

        call_obj = self._calls[call_id]
        pc = call_obj.pc

        @pc.on("track")
        async def on_track(track):
            logger.info(f"Track {track.kind} received")
            if track.kind == "audio":
                call_obj.player = MediaPlayer("/dev/zero", format="s16le", options={"ar": "8000", "ac": "1"})
                pc.addTrack(self._relay.subscribe(call_obj.player.audio))

            @track.on("ended")
            async def on_ended():
                logger.info(f"Track {track.kind} ended")
                await self._hangup(call_id)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info(f"Connection state is {pc.connectionState}")
            if pc.connectionState == "failed":
                await self._hangup(call_id)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=remote_sdp, type="offer"))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        local_sdp = pc.localDescription.sdp
        await self._calls[call_id].call.perform(Answer(sdp=local_sdp))
        await self._event_bus.emit("SIP_CALL_ANSWERED", self._calls[call_id].call.remote_uri)

    async def _hangup(self, call_id: str):
        if call_id not in self._calls:
            return

        logger.info(f"Hanging up call {call_id}")
        call_obj = self._calls.pop(call_id)
        
        if call_obj.call.state != "disconnected":
            logger.info(f"Performing Hangup() action for call {call_id}")
            await call_obj.call.perform(Hangup())
        else:
            logger.info(f"Call {call_id} already disconnected, skipping Hangup() action.")

        if call_obj.player:
            logger.info(f"Stopping audio player for call {call_id}")
            call_obj.player.audio.stop()
        
        logger.info(f"Closing RTCPeerConnection for call {call_id}")
        await call_obj.pc.close()
        logger.info(f"RTCPeerConnection closed for call {call_id}")
        await self._event_bus.emit("SIP_CALL_ENDED", {"reason": "hangup", "addr": call_obj.call.remote_uri, "was_active": True})
        logger.info(f"SIP_CALL_ENDED event emitted for call {call_id}")

    async def _check_rtp_activity(self):
        while self._pysip_server.is_running():
            await asyncio.sleep(1)
            now = time.time()
            for call_id, call_obj in list(self._calls.items()):
                if call_obj.pc.connectionState == "connected":
                    stats = await call_obj.pc.getStats()
                    logger.debug(f"RTP stats for call {call_id}: {stats}")
                    for stat in stats.values():
                        if stat.type == "inbound-rtp":
                            logger.debug(f"Inbound RTP stat: {stat}")
                            if stat.bytesReceived > call_obj.last_bytes_received:
                                logger.debug(f"New bytes received: {stat.bytesReceived - call_obj.last_bytes_received}")
                                call_obj.last_rtp_activity = now
                                call_obj.last_bytes_received = stat.bytesReceived
                                break
                    
                    if now - call_obj.last_rtp_activity > 3:
                        logger.warning(f"No RTP activity on call {call_id}, hanging up.")
                        await self._hangup(call_id)
