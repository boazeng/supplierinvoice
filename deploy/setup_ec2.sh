#!/bin/bash
# EC2 Setup Script for Supplier Invoice System
# Run this on a fresh Amazon Linux 2023 / Ubuntu instance

set -e

echo "=== Installing dependencies ==="
# Detect OS
if command -v dnf &> /dev/null; then
    # Amazon Linux 2023
    sudo dnf update -y
    sudo dnf install -y python3.11 python3.11-pip git
    PYTHON=python3.11
elif command -v apt &> /dev/null; then
    # Ubuntu
    sudo apt update -y
    sudo apt install -y python3 python3-pip python3-venv git
    PYTHON=python3
fi

echo "=== Cloning repository ==="
cd /home/ec2-user 2>/dev/null || cd /home/ubuntu
git clone https://github.com/boazeng/supplierinvoice.git
cd supplierinvoice

echo "=== Setting up virtual environment ==="
$PYTHON -m venv venv
source venv/bin/activate

echo "=== Installing Python packages ==="
pip install --upgrade pip
pip install -r requirements.txt

echo "=== Creating directories ==="
mkdir -p data/invoices data/cache logs database

echo "=== Creating .env file (EDIT THIS!) ==="
cp .env.example .env
echo ""
echo "============================================"
echo "  IMPORTANT: Edit the .env file with your"
echo "  actual API keys and credentials:"
echo ""
echo "  nano .env"
echo ""
echo "============================================"

echo "=== Setting up systemd service ==="
sudo tee /etc/systemd/system/supplierinvoice.service > /dev/null << 'SERVICEEOF'
[Unit]
Description=Supplier Invoice System
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/supplierinvoice
Environment=PATH=/home/ec2-user/supplierinvoice/venv/bin:/usr/bin
ExecStart=/home/ec2-user/supplierinvoice/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable supplierinvoice

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "1. Edit .env:  nano .env"
echo "2. Start:      sudo systemctl start supplierinvoice"
echo "3. Check:      sudo systemctl status supplierinvoice"
echo "4. Logs:       journalctl -u supplierinvoice -f"
echo ""
