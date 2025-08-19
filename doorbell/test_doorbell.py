#!/usr/bin/env python3
"""
Test script for doorbell system functionality.
Tests HTTP API, UDP messages, and SIP integration.
"""
import requests
import socket
import json
import time
import sys

class DoorbellTester:
    def __init__(self, host="localhost", http_port=8000, udp_port=9999, sip_port=5060):
        self.host = host
        self.http_port = http_port
        self.udp_port = udp_port
        self.sip_port = sip_port
        self.base_url = f"http://{host}:{http_port}"
        
    def test_http_status(self):
        """Test HTTP status endpoint"""
        print("Testing HTTP status endpoint...")
        try:
            response = requests.get(f"{self.base_url}/status")
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Status: {json.dumps(data, indent=2)}")
                return True
            else:
                print(f"❌ HTTP error: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Connection error: {e}")
            return False
            
    def test_health_check(self):
        """Test health check endpoint"""
        print("Testing health check endpoint...")
        try:
            response = requests.get(f"{self.base_url}/healthz")
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Health: {json.dumps(data, indent=2)}")
                return True
            else:
                print(f"❌ Health check failed: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Health check error: {e}")
            return False
            
    def test_image_endpoint(self):
        """Test image endpoint"""
        print("Testing image endpoint...")
        try:
            response = requests.get(f"{self.base_url}/image")
            if response.status_code == 200:
                print(f"✅ Image: {len(response.content)} bytes")
                return True
            else:
                print(f"❌ Image error: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Image error: {e}")
            return False
            
    def test_udp_command(self, command="STATUS"):
        """Test UDP command"""
        print(f"Testing UDP command: {command}")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(5)
            
            # Send command
            sock.sendto(command.encode(), (self.host, self.udp_port))
            
            # Wait for response
            data, addr = sock.recvfrom(1024)
            response = data.decode()
            print(f"✅ UDP response: {response}")
            sock.close()
            return True
            
        except Exception as e:
            print(f"❌ UDP error: {e}")
            return False
            
    def test_sip_hangup(self):
        """Test SIP hangup endpoint"""
        print("Testing SIP hangup endpoint...")
        try:
            response = requests.post(f"{self.base_url}/sip/hangup")
            if response.status_code == 200:
                data = response.json()
                print(f"✅ SIP hangup: {data}")
                return True
            else:
                print(f"❌ SIP hangup error: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ SIP hangup error: {e}")
            return False
            
    def run_all_tests(self):
        """Run all tests"""
        print(f"Testing doorbell system at {self.host}")
        print("=" * 50)
        
        tests = [
            ("HTTP Status", self.test_http_status),
            ("Health Check", self.test_health_check),
            ("Image Endpoint", self.test_image_endpoint),
            ("UDP Status", lambda: self.test_udp_command("STATUS")),
            ("UDP Ping", lambda: self.test_udp_command("PING")),
            ("SIP Hangup", self.test_sip_hangup),
        ]
        
        results = []
        for name, test_func in tests:
            print(f"\n{name}:")
            try:
                result = test_func()
                results.append((name, result))
            except Exception as e:
                print(f"❌ {name} failed: {e}")
                results.append((name, False))
                
        print("\n" + "=" * 50)
        print("TEST RESULTS:")
        passed = 0
        for name, result in results:
            status = "✅ PASS" if result else "❌ FAIL"
            print(f"{name}: {status}")
            if result:
                passed += 1
                
        print(f"\nPassed: {passed}/{len(results)}")
        return passed == len(results)

if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    tester = DoorbellTester(host)
    success = tester.run_all_tests()
    sys.exit(0 if success else 1)
