.PHONY: help install uninstall

help:
	@echo "Commands:"
	@echo "  install    - Install the doorbell systemd service. Must be run with sudo."
	@echo "  uninstall  - Uninstall the doorbell systemd service. Must be run with sudo."

install:
	@echo "Running installation script..."
	sudo uv run python install.py

uninstall:
	@echo "Running uninstallation script..."
	sudo uv run python uninstall.py