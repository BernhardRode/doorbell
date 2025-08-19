#!/usr/bin/env python3
import os
import subprocess
import sys

def uninstall_systemd_service():
    """Stop, disable and remove the doorbell systemd service."""
    service_name = 'doorbell.service'
    service_path = f'/etc/systemd/system/{service_name}'
    
    try:
        # Stop service if running
        subprocess.run(['systemctl', 'stop', service_name], check=False)
        
        # Disable service
        subprocess.run(['systemctl', 'disable', service_name], check=False)
        
        # Remove service file
        if os.path.exists(service_path):
            os.remove(service_path)
        
        # Reload systemd
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        
        print("Doorbell service uninstalled successfully!")
        
    except PermissionError:
        print("Warning: Could not uninstall systemd service (requires root)")
        print(f"Manually remove {service_path}")
    except Exception as e:
        print(f"Warning: Service uninstallation failed: {e}")

if __name__ == '__main__':
    uninstall_systemd_service()
