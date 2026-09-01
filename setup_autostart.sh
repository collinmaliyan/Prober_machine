#!/bin/bash
# ==============================================================================
# Prober Machine Work - Auto-Start Setup Script for Linux Yocto / i.MX8
# ==============================================================================
# Usage:
#   chmod +x setup_autostart.sh
#   bash setup_autostart.sh
# ==============================================================================

echo "======================================================="
echo "  Setting up Prober Machine Auto-Start (systemd service) "
echo "======================================================="

APP_DIR=$(cd "$(dirname "$0")" && pwd)
PYTHON_BIN=$(which python3 || echo "/usr/bin/python3")
SERVICE_FILE="/etc/systemd/system/prober.service"

echo "[INFO] Project Directory: $APP_DIR"
echo "[INFO] Python Binary:     $PYTHON_BIN"
echo "[INFO] Target Service:    $SERVICE_FILE"

# 1. Ensure serial port permissions
echo "[INFO] Setting up serial port access permissions..."
usermod -a -G dialout root 2>/dev/null || true

# 2. Generate systemd service configuration
echo "[INFO] Writing systemd service configuration..."
cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Prober Machine RFID Flask Application
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
ExecStart=$PYTHON_BIN $APP_DIR/Main_Prober_with_error.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 3. Reload systemd daemon
echo "[INFO] Reloading systemd daemon..."
systemctl daemon-reload

# 4. Enable service on boot
echo "[INFO] Enabling prober.service on boot..."
systemctl enable prober.service

# 5. Restart service now
echo "[INFO] Starting prober.service now..."
systemctl restart prober.service

echo "======================================================="
echo "  Prober Service Setup Complete! Current Status:      "
echo "======================================================="
systemctl status prober.service --no-pager
