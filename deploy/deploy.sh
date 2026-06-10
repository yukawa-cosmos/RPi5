#!/bin/bash
# ぴっぴ Access デプロイスクリプト（開発マシン → ラズパイ）
# 使い方: PI_HOST=pi@192.168.1.xx bash deploy/deploy.sh
set -euo pipefail

PI_HOST="${PI_HOST:-pi@raspberrypi.local}"
REMOTE_DIR="/home/pi/cosmos-robot"
SERVICE_NAME="pippi-robot"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== ぴっぴ Access デプロイ ==="
echo "転送先: $PI_HOST:$REMOTE_DIR"
echo ""

# コードを転送（.env・DB・キャッシュは転送しない）
rsync -av --delete \
  --exclude '.git/' \
  --exclude 'venv/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.env' \
  --exclude 'data/staff_embeddings.json' \
  --exclude 'pippi.db' \
  "$PROJECT_DIR/" "$PI_HOST:$REMOTE_DIR/"

# サービス再起動
echo ""
echo "サービスを再起動中..."
ssh "$PI_HOST" "sudo systemctl restart $SERVICE_NAME"
sleep 2
ssh "$PI_HOST" "sudo systemctl status $SERVICE_NAME --no-pager -l"

echo ""
echo "=== デプロイ完了 ==="
