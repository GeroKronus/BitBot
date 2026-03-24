#!/bin/bash
# BitBot deploy script — uploads to Lightsail and restarts service

SERVER="ubuntu@52.73.39.197"
KEY="D:/Claude Code/Openclaw/openclaw-key.pem"
REMOTE_DIR="/home/ubuntu/gridbot"
SSH_OPTS="-o StrictHostKeyChecking=no"

echo "=== BitBot Deploy ==="

# Create remote directory
ssh -i "$KEY" $SSH_OPTS "$SERVER" "mkdir -p $REMOTE_DIR/data"

# Upload project files
scp -i "$KEY" $SSH_OPTS -r \
    gridbot/ config.json requirements.txt gridbot.service \
    "$SERVER:$REMOTE_DIR/"

echo "Files uploaded."

# Install dependencies and restart service
ssh -i "$KEY" $SSH_OPTS "$SERVER" << 'EOF'
cd /home/ubuntu/gridbot
pip3 install -r requirements.txt --quiet
sudo cp gridbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable gridbot
sudo systemctl restart gridbot
sleep 2
sudo systemctl status gridbot --no-pager
echo ""
echo "BitBot deployed successfully!"
EOF
