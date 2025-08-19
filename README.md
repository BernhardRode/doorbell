# Doorbell System

A Raspberry Pi doorbell system with SIP calling capabilities, video streaming, and GPIO button handling.

## Quick Start

### Install and Run with uvx (Recommended)

```bash
# Install and run directly from GitHub
uvx --from git+https://github.com/bernhardrode/doorbell doorbell

# Or install from PyPI (when published)
uvx doorbell
```

### Manual Installation

```bash
# Clone and install
git clone https://github.com/bernhardrode/doorbell.git
cd doorbell
pip install .

# Run
doorbell
```

### Systemd Service (Raspberry Pi)

After installation, set up the systemd service to run doorbell automatically:

```bash
# Install and enable the service (requires sudo)
sudo doorbell-install-service

# Start the service
sudo systemctl start doorbell

# Check status
sudo systemctl status doorbell

# View logs
sudo journalctl -u doorbell -f

# Uninstall the service (requires sudo)
sudo doorbell-uninstall-service
```

## Configuration

Create a config file at `~/.config/doorbell.json`:

```json
{
  "axis_hostname": "192.168.1.27",
  "axis_username": "loxone", 
  "axis_password": "your-password",
  "active_timeout": 15,
  "button_pins": [16, 20, 21],
  "led_pin": 17,
  "sip_enabled": true,
  "sip_port": 5060
}
```

Or use environment variables:
```bash
export AXIS_HOSTNAME=192.168.1.27
export AXIS_PASSWORD=your-password
doorbell
```

## Features

### Core Features
- **GPIO Button Handling**: Three buttons connected to GPIO pins
- **UDP Broadcasting**: Sends messages when buttons are pressed
- **Video Streaming**: Proxy to Axis camera MJPEG stream
- **Image Caching**: Dynamic caching (60s normal, 2s when active)
- **LED Status**: Visual indicator for active state
- **HTTP API**: RESTful endpoints for status and control

### SIP Features
- **SIP Server**: Accepts incoming calls on port 5060
- **Call Management**: Proper SIP protocol handling (INVITE, BYE, ACK)
- **Active State Integration**: Stays active during calls
- **Auto-answer**: Automatically answers calls after 3 seconds
- **Call Timeout**: 60-second maximum call duration
- **Manual Hangup**: API endpoint to terminate calls

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | System status including SIP call state |
| `/image` | GET | Latest cached camera image (JPEG) |
| `/video` | GET | Live camera video stream (MJPEG) |
| `/sip/hangup` | POST | Manually hang up active SIP call |
| `/healthz` | GET | Health check |

## Hardware Requirements

- **Raspberry Pi** (3B+ or newer recommended)
- **3 Push Buttons** connected to GPIO
- **1 LED** for status indication
- **Axis Network Camera** with MJPEG support
- **Network Connection** for camera and SIP clients

## Development

```bash
# Clone repository
git clone https://github.com/bernhardrode/doorbell.git
cd doorbell

# Install in development mode
pip install -e ".[dev]"

# Run tests
python -m doorbell.test_doorbell
python -m doorbell.test_sip_client
```

## License

MIT License - see LICENSE file for details.