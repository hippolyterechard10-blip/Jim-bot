#!/bin/bash
set -e
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl
cd /opt
if [ -d "Jim-bot" ]; then cd Jim-bot && git pull origin main; else git clone https://github.com/hippolyterechard10-blip/Jim-bot.git && cd Jim-bot; fi
cd trading-agent
python3 -m venv /opt/jimbot-venv
/opt/jimbot-venv/bin/pip install --upgrade pip
/opt/jimbot-venv/bin/pip install -r requirements.txt
cat > /etc/systemd/system/jimbot.service << 'EOF'
[Unit]
Description=Jim Bot Trading Agent
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=root
WorkingDirectory=/opt/Jim-bot/trading-agent
ExecStart=/opt/jimbot-venv/bin/python main_bybit.py
Restart=on-failure
RestartSec=10
StandardOutput=append:/var/log/jimbot.log
StandardError=append:/var/log/jimbot.log
EnvironmentFile=/opt/Jim-bot/trading-agent/.env
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable jimbot
echo "DONE - now create /opt/Jim-bot/trading-agent/.env then run: systemctl start jimbot"
