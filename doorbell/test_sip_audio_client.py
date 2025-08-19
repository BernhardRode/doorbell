#!/usr/bin/env python3
"""
SIP client with TTS audio streaming for doorbell testing.
Sends text-to-speech audio via RTP to the doorbell.
"""
import socket
import time
import uuid
import struct
import threading
import sys

try:
    from TTS import TTS
    import pyaudio
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False
    print("TTS or PyAudio not available. Install with: pip install TTS pyaudio")

class SIPAudioClient:
    def __init__(self, server_host="localhost", server_port=5060):
        self.server_host = server_host
        self.server_port = server_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.call_id = str(uuid.uuid4())
        self.rtp_socket = None
        self.rtp_port = None
        self.remote_rtp_port = None
        self.sequence = 0
        self.timestamp = 0
        self.ssrc = 12345
        
        if TTS_AVAILABLE:
            self.tts = TTS(engine="espeak")
            self.tts.lang("en-US")
        else:
            self.tts = None
        
    def setup_rtp(self):
        """Setup RTP socket for audio streaming"""
        self.rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_socket.bind(('0.0.0.0', 0))
        self.rtp_port = self.rtp_socket.getsockname()[1]
        print(f"RTP socket bound to port {self.rtp_port}")
        
    def send_invite_with_sdp(self):
        """Send INVITE with SDP for audio"""
        self.setup_rtp()
        
        # Create SDP
        local_ip = socket.gethostbyname(socket.gethostname())
        sdp = f"""v=0
o=client 0 0 IN IP4 {local_ip}
s=Test Call
c=IN IP4 {local_ip}
t=0 0
m=audio {self.rtp_port} RTP/AVP 0
a=rtpmap:0 PCMU/8000"""

        invite = f"""INVITE sip:door@{self.server_host}:{self.server_port} SIP/2.0
Via: SIP/2.0/UDP {self.server_host}:{self.server_port}
From: <sip:test@{self.server_host}>
To: <sip:door@{self.server_host}>
Call-ID: {self.call_id}
CSeq: 1 INVITE
Content-Type: application/sdp
Content-Length: {len(sdp)}

{sdp}"""
        
        self.sock.sendto(invite.encode(), (self.server_host, self.server_port))
        print(f"Sent INVITE with SDP to {self.server_host}:{self.server_port}")
        
    def parse_sdp_response(self, response):
        """Parse SDP from 200 OK response to get remote RTP port"""
        lines = response.split('\n')
        for line in lines:
            if line.startswith('m=audio'):
                parts = line.split()
                if len(parts) >= 2:
                    self.remote_rtp_port = int(parts[1])
                    print(f"Remote RTP port: {self.remote_rtp_port}")
                    break
                    
    def create_rtp_packet(self, payload):
        """Create RTP packet with G.711 PCMU payload"""
        # RTP Header (12 bytes)
        version = 2
        padding = 0
        extension = 0
        cc = 0
        marker = 0
        payload_type = 0  # PCMU
        
        # Pack RTP header
        header = struct.pack('!BBHII',
            (version << 6) | (padding << 5) | (extension << 4) | cc,
            (marker << 7) | payload_type,
            self.sequence,
            self.timestamp,
            self.ssrc
        )
        
        self.sequence = (self.sequence + 1) % 65536
        self.timestamp += len(payload)
        
        return header + payload
        
    def text_to_pcmu(self, text):
        """Convert text to G.711 PCMU audio data"""
        if not self.tts:
            return b''
            
        try:
            # Generate audio file
            import tempfile
            import wave
            import os
            
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                tmp_path = tmp.name
                
            # Generate TTS audio
            self.tts.tts_to_file(text=text, file_path=tmp_path)
            
            # Read WAV file and convert to PCMU
            with wave.open(tmp_path, 'rb') as wav:
                frames = wav.readframes(wav.getnframes())
                
            # Clean up
            os.unlink(tmp_path)
            
            # Simple conversion to 8kHz mono (very basic)
            # In production, use proper audio conversion
            return frames[::4]  # Downsample roughly
            
        except Exception as e:
            print(f"TTS conversion error: {e}")
            return b''
            
    def stream_audio(self, text):
        """Stream TTS audio via RTP"""
        if not self.remote_rtp_port or not self.rtp_socket:
            print("RTP not setup")
            return
            
        print(f"Streaming TTS: '{text}'")
        audio_data = self.text_to_pcmu(text)
        
        if not audio_data:
            print("No audio data generated")
            return
            
        # Send audio in chunks
        chunk_size = 160  # 20ms at 8kHz
        for i in range(0, len(audio_data), chunk_size):
            chunk = audio_data[i:i+chunk_size]
            if len(chunk) < chunk_size:
                chunk += b'\x00' * (chunk_size - len(chunk))  # Pad with silence
                
            rtp_packet = self.create_rtp_packet(chunk)
            self.rtp_socket.sendto(rtp_packet, (self.server_host, self.remote_rtp_port))
            time.sleep(0.02)  # 20ms delay
            
        print("Audio streaming complete")
        
    def listen_for_responses(self, timeout=15):
        """Listen for SIP responses"""
        self.sock.settimeout(timeout)
        try:
            while True:
                data, addr = self.sock.recvfrom(2048)
                response = data.decode()
                print(f"Received from {addr}:")
                print(response)
                print("-" * 40)
                
                # Check for 200 OK
                if "200 OK" in response:
                    print("Call answered!")
                    self.parse_sdp_response(response)
                    return True
                elif "486 Busy" in response:
                    print("Line busy!")
                    return False
                    
        except socket.timeout:
            print("No response received")
            return False
            
    def test_call_with_audio(self, message="Hello from the doorbell test client!"):
        """Test complete call flow with TTS audio"""
        print("Starting SIP call with audio test...")
        
        if not TTS_AVAILABLE:
            print("TTS not available - audio streaming disabled")
            return
        
        # Send INVITE with SDP
        self.send_invite_with_sdp()
        
        # Listen for responses
        answered = self.listen_for_responses(15)
        
        if answered and self.remote_rtp_port:
            print("Call established, streaming audio...")
            
            # Stream TTS audio
            self.stream_audio(message)
            
            print("Waiting 3 seconds...")
            time.sleep(3)
            
            # Send BYE
            self.send_bye()
            self.listen_for_responses(5)
            
        if self.rtp_socket:
            self.rtp_socket.close()
        self.sock.close()
        print("Test complete")
        
    def send_bye(self):
        """Send BYE to end call"""
        bye = f"""BYE sip:door@{self.server_host}:{self.server_port} SIP/2.0
Via: SIP/2.0/UDP {self.server_host}:{self.server_port}
From: <sip:test@{self.server_host}>
To: <sip:door@{self.server_host}>
Call-ID: {self.call_id}
CSeq: 2 BYE
Content-Length: 0

"""
        self.sock.sendto(bye.encode(), (self.server_host, self.server_port))
        print(f"Sent BYE to {self.server_host}:{self.server_port}")

if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    message = sys.argv[2] if len(sys.argv) > 2 else "Hello from the doorbell test client!"
    
    client = SIPAudioClient(host)
    client.test_call_with_audio(message)
