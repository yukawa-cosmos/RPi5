#!/bin/bash
set -e

echo "=== ぴっぴ クライアント セットアップ ==="

# apt パッケージ
sudo apt install -y python3-opencv libzbar0 libportaudio2

# venv（システム site-packages を引き継ぐ = apt の cv2 が使える）
python3 -m venv --system-site-packages .venv
source .venv/bin/activate

# pip パッケージ
pip install websockets aiohttp numpy sounddevice soundfile python-dotenv psutil pyzbar

# .env
if [ ! -f .env ]; then
    cp .env.example .env
    echo ".env を編集して PIPPI_API_KEY を設定してください"
fi

echo "=== 完了 ==="
echo "次: nano .env  →  source .venv/bin/activate  →  python3 check_cameras.py"
