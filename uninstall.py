import subprocess
import os

SERVICE_NAME = "doorbell"
INSTALL_DIR = "/opt/doorbell"
SERVICE_USER = "doorbell"

def main():
    if os.geteuid() != 0:
        print("This script must be run with sudo.")
        return

    # Stop and disable service
    print(f"Stopping and disabling {SERVICE_NAME} service...")
    subprocess.run(["systemctl", "stop", SERVICE_NAME], check=False)
    subprocess.run(["systemctl", "disable", SERVICE_NAME], check=False)

    # Remove service file
    service_file = f"/etc/systemd/system/{SERVICE_NAME}.service"
    if os.path.exists(service_file):
        print(f"Removing {service_file}...")
        os.remove(service_file)

    # Reload systemd
    print("Reloading systemd...")
    subprocess.run(["systemctl", "daemon-reload"], check=True)

    # Remove installation directory
    if os.path.exists(INSTALL_DIR):
        print(f"Removing installation directory {INSTALL_DIR}...")
        subprocess.run(["rm", "-rf", INSTALL_DIR], check=True)

    # Remove user
    print(f"To remove the {SERVICE_USER} user, run: sudo userdel {SERVICE_USER}")

    print("Uninstallation complete.")

if __name__ == "__main__":
    main()
