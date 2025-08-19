#!/usr/bin/env python3
"""
Simple SIP client test script for doorbell system.
Tests basic SIP functionality without audio.
"""
import socket
import time
import uuid

class SimpleSIPClient:
    def __init__(self, server_host="localhost", server_port=5060):
        self.server_host = server_host
        self.server_port = server_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.call_id = str(uuid.uuid4())
        
    def send_invite(self):
        """Send INVITE to doorbell"""
        invite = f"""INVITE sip:door@{self.server_host}:{self.server_port} SIP/2.0
Via: SIP/2.0/UDP {self.server_host}:{self.server_port}
From: <sip:test@{self.server_host}>
To: <sip:door@{self.server_host}>
Call-ID: {self.call_id}
CSeq: 1 INVITE
Content-Length: 0

"""
        self.sock.sendto(invite.encode(), (self.server_host, self.server_port))
        print(f"Sent INVITE to {self.server_host}:{self.server_port}")
        
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
        
    def listen_for_responses(self, timeout=10):
        """Listen for SIP responses"""
        self.sock.settimeout(timeout)
        try:
            while True:
                data, addr = self.sock.recvfrom(1024)
                response = data.decode()
                print(f"Received from {addr}:")
                print(response)
                print("-" * 40)
                
                # Check for 200 OK
                if "200 OK" in response:
                    print("Call answered!")
                    return True
                elif "486 Busy" in response:
                    print("Line busy!")
                    return False
                    
        except socket.timeout:
            print("No response received")
            return False
            
    def test_call(self):
        """Test complete call flow"""
        print("Starting SIP call test...")
        
        # Send INVITE
        self.send_invite()
        
        # Listen for responses
        answered = self.listen_for_responses(15)
        
        if answered:
            print("Waiting 5 seconds...")
            time.sleep(5)
            
            # Send BYE
            self.send_bye()
            self.listen_for_responses(5)
            
        self.sock.close()
        print("Test complete")

if __name__ == "__main__":
    import sys
    
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    client = SimpleSIPClient(host)
    client.test_call()
