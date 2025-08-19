#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys

def install_systemd_service():
    """Install and enable the doorbell systemd service."""
    service_file = os.path.join(os.path.dirname(__file__), 'doorbell.service')
    target_path = '/etc/systemd/system/doorbell.service'
    
    try:
        # Copy service file
        shutil.copy2(service_file, target_path)
        
        # Reload systemd and enable service
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'enable', 'doorbell.service'], check=True)
        
        print("Doorbell service installed and enabled successfully!")
        print("Start with: sudo systemctl start doorbell")
        
    except PermissionError:
        print("Warning: Could not install systemd service (requires root)")
        print(f"Manually copy {service_file} to {target_path}")
    except subprocess.CalledProcessError as e:
        print(f"Warning: systemctl command failed: {e}")
    except Exception as e:
        print(f"Warning: Service installation failed: {e}")

if __name__ == '__main__':
    install_systemd_service()
