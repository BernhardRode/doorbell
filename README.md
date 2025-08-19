# Doorbell System

This project implements a doorbell system using a Raspberry Pi. The system consists of three buttons that, when pressed, send a UDP broadcast with the corresponding button number.

## Project Structure

```
doorbell-system
├── src
│   ├── main.py               # Entry point of the application
│   ├── doorbell
│   │   ├── __init__.py       # Marks the directory as a Python package
│   │   ├── buttons.py         # Button class for handling button events
│   │   └── udp_broadcast.py   # Function to send UDP broadcasts
├── requirements.txt           # Project dependencies
└── README.md                  # Project documentation
```

## Setup Instructions

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd doorbell-system
   ```

2. **Install dependencies:**
   Make sure you have Python and uv installed. Then run:
   ```bash
   uv sync
   ```

3. **Connect the buttons:**
   Connect three buttons to the GPIO pins of your Raspberry Pi.

4. **Run the application:**
   Execute the main script:
   ```bash
   uv run main.py
   ```

## Application Features

When a button is pressed, the system will send a UDP broadcast message containing the button number (1, 2, or 3). Ensure that your network allows UDP broadcasts for the messages to be received by other devices.

we have two proxy endpoints /video or /image they proxy to a axis camera with basic auth (configure in ENVIRONMENT variables)

http://192.168.1.27/axis-cgi/jpg/image.cgi?&camera=1
http://192.168.1.27/axis-cgi/mjpg/video.cgi

After the door bell rings, we go into active mode. Active Means, we wait for somebody in the house to open their Loxone App. 

Also we have to provide a SIP server, where our clients can connect to and speak with a person in front of the door using

If nobody answers the call within one minute, we should store the last recorded minute to a file, stop and hangup.

## License

This project is licensed under the MIT License. See the LICENSE file for details.