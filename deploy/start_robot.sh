#!/bin/bash
# ぴっぴ Access（ラズパイ側）起動スクリプト
# ラズパイ起動時に自動実行されます（pippi-robot.service から呼ばれる）

GYOMU_URL="${GYOMU_WS_URL:-ws://localhost:8001/ws/robot}"
# プロジェクトルートはこのスクリプト(deploy/)の1つ上
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
[ -x "$VENV_PYTHON" ] || VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
[ -x "$VENV_PYTHON" ] || VENV_PYTHON="python3"
# クライアントコードは client/（pippi_client パッケージ）
WORKDIR="$PROJECT_DIR/client"

# GYOMU_URL から接続先 host:port を抽出 (ws://host:port/path → host port)
# wss:// を先に除去しないと ws:// 除去で "s://..." が残るバグになる
_ws="${GYOMU_URL#wss://}"
_ws="${_ws#ws://}"
_hostport="${_ws%%/*}"
GYOMU_HOST="${_hostport%%:*}"
GYOMU_PORT="${_hostport##*:}"
[ "$GYOMU_PORT" = "$_hostport" ] && GYOMU_PORT="8001"

# GyomuSystem が起動するまで最大60秒待つ
echo "[access] GyomuSystem (${GYOMU_HOST}:${GYOMU_PORT}) 起動を待機中..."
for i in $(seq 1 30); do
  if timeout 2 bash -c "echo >/dev/tcp/${GYOMU_HOST}/${GYOMU_PORT}" 2>/dev/null; then
    echo "[access] サーバー起動確認 (${i}回目)"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "[access] サーバーが起動しませんでした。終了します。"
    exit 1
  fi
  sleep 2
done

# 検知方式とカメラ送信先（.env で上書き可）
#   PIPPI_DETECTOR 省略時は mediapipe（カメラ検知）。mock にすると実センサー無効
#   PIPPI_CAM_URL  省略時は GYOMU_HOST の 8000 番から自動導出（来訪者顔照合フレーム送信先）
DETECTOR="${PIPPI_DETECTOR:-mediapipe}"
CAM_URL="${PIPPI_CAM_URL:-http://${GYOMU_HOST}:8000/api/camera/push}"

echo "[access] Access（クライアント）を起動します... (detector=${DETECTOR})"
cd "$WORKDIR"
exec "$VENV_PYTHON" -m pippi_client.access \
  --gyomu-url "$GYOMU_URL" \
  --detector "$DETECTOR" \
  --cam-url "$CAM_URL"
