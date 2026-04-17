#!/bin/bash
# deploy.sh — Jim Bot Kraken Futures, VPS systemd install
# Usage (root) :
#   curl -fsSL https://raw.githubusercontent.com/hippolyterechard10-blip/Jim-bot/main/deploy.sh | bash
set -euo pipefail

REPO_URL="https://github.com/hippolyterechard10-blip/Jim-bot.git"
BRANCH="${BRANCH:-main}"
APP_DIR="/opt/Jim-bot"
VENV_DIR="/opt/jimbot-venv"
ENV_FILE="$APP_DIR/trading-agent/.env"
SERVICE="/etc/systemd/system/jimbot.service"
USER_NAME="jimbot"

echo "==> apt deps"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl ca-certificates

echo "==> dedicated user"
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$USER_NAME"
fi

echo "==> clone / pull"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

echo "==> venv + requirements"
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip wheel
"$VENV_DIR/bin/pip" install -r "$APP_DIR/trading-agent/requirements.txt"

echo "==> .env template"
if [ ! -f "$ENV_FILE" ]; then
  cp "$APP_DIR/trading-agent/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "   ⚠️  Édite $ENV_FILE avec tes clés Kraken Futures avant de démarrer."
fi

echo "==> permissions"
chown -R "$USER_NAME:$USER_NAME" "$APP_DIR"
chown -R "$USER_NAME:$USER_NAME" "$VENV_DIR"
touch /var/log/jimbot.log
chown "$USER_NAME:$USER_NAME" /var/log/jimbot.log

echo "==> systemd unit"
cat > "$SERVICE" <<EOF
[Unit]
Description=Jim Bot — GEO strategy on Kraken Futures
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
WorkingDirectory=$APP_DIR/trading-agent
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python main_kraken.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/jimbot.log
StandardError=append:/var/log/jimbot.log

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/trading-agent /var/log/jimbot.log
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable jimbot

cat <<MSG
✅ Installé.
Étapes suivantes :
  1. Remplis les clés dans $ENV_FILE  (chmod 600)
  2. systemctl start jimbot
  3. journalctl -u jimbot -f     # ou tail -f /var/log/jimbot.log

Mode paper par défaut (KRAKEN_PAPER=1). Passe à 0 une fois validé.
MSG
