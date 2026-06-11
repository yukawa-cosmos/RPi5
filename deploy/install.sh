#!/bin/bash
# ぴっぴ Access セットアップスクリプト（ラズパイ用）
# 使い方: cd /home/pi/cosmos-robot && bash deploy/install.sh
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="pippi-robot"

echo "=== ぴっぴ Access セットアップ ==="
echo "インストール先: $INSTALL_DIR"

# 1. システムパッケージ
echo ""
echo "[1/4] システムパッケージをインストール中..."
sudo apt-get update -q
sudo apt-get install -y \
  python3-pip python3-venv \
  portaudio19-dev libportaudio2 \
  libasound2-dev \
  libopencv-dev python3-opencv \
  curl

# 2. Python 仮想環境
echo ""
echo "[2/4] Python 仮想環境を作成中..."
cd "$INSTALL_DIR"
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
echo "  完了"

# 3. .env セットアップ
echo ""
echo "[3/4] 環境変数ファイルを確認..."
if [ ! -f "$INSTALL_DIR/.env" ]; then
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  echo "  .env を作成しました。以下を設定してください:"
  echo "  GYOMU_WS_URL=ws://<サーバーIPアドレス>:8091/ws/robot"
else
  echo "  .env は既に存在します。"
fi

# 4. systemd サービス登録
echo ""
echo "[4/4] systemd サービスを登録中..."
sudo cp "$INSTALL_DIR/deploy/pippi-robot.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "  $SERVICE_NAME を自動起動に設定しました"

echo ""
echo "=== セットアップ完了 ==="
echo ""
echo "次の手順:"
echo "  1. .env を編集: nano $INSTALL_DIR/.env"
echo "     └ GYOMU_WS_URL=ws://<サーバーIPアドレス>:8091/ws/robot"
echo "     └ PIPPI_API_KEY=<管理画面 → 設定タブ → APIキー欄の値>"
echo "  2. 起動: sudo systemctl start $SERVICE_NAME"
echo "  3. 確認: sudo systemctl status $SERVICE_NAME"
