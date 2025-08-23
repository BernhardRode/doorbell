import subprocess
import os

SERVICE_USER = "doorbell"
INSTALL_DIR = "/opt/doorbell"
SERVICE_NAME = "doorbell"

def main():
    if os.geteuid() != 0:
        print("This script must be run with sudo.")
        return

    # Create user
    print(f"Creating user {SERVICE_USER}...")
    subprocess.run(["useradd", "--system", "--home-dir", INSTALL_DIR, "--no-create-home", SERVICE_USER], check=False)
    print(f"Adding user {SERVICE_USER} to gpio group...")
    subprocess.run(["usermod", "-aG", "gpio", SERVICE_USER], check=True)

    # Create install dir
    print(f"Creating installation directory {INSTALL_DIR}...")
    os.makedirs(INSTALL_DIR, exist_ok=True)
    
    # Set ownership for installation
    sudo_user = os.environ["SUDO_USER"]
    print(f"Setting ownership of {INSTALL_DIR} for installation by {sudo_user}...")
    subprocess.run(["chown", f"{sudo_user}:{sudo_user}", INSTALL_DIR], check=True)

    # Install package
    print(f"Installing doorbell package to {INSTALL_DIR}...")
    subprocess.run(["sudo", "-u", sudo_user, "uv", "pip", "install", ".", "--target", INSTALL_DIR], check=True)

    # Set ownership for service
    print(f"Setting ownership of {INSTALL_DIR} for service...")
    subprocess.run(["chown", "-R", f"{SERVICE_USER}:{SERVICE_USER}", INSTALL_DIR], check=True)

    # Create service file
    print("Creating systemd service file...")
    service_file_content = f"""
[Unit]
Description=Doorbell Service
After=network.target

[Service]
Type=simple
User={SERVICE_USER}
Group={SERVICE_USER}
Environment="LOG_LEVEL=DEBUG"
ExecStart={INSTALL_DIR}/bin/doorbell
WorkingDirectory={INSTALL_DIR}
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
"""
    with open(f"/etc/systemd/system/{SERVICE_NAME}.service", "w") as f:
        f.write(service_file_content.strip())

    # Reload and enable service
    print("Reloading systemd...")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("Enabling and starting doorbell service...")
    subprocess.run(["systemctl", "enable", "--now", SERVICE_NAME], check=True)

    print("Installation complete.")

if __name__ == "__main__":
    main()
