"""接続中のカメラ一覧を表示する"""
import cv2

MAX_INDEX = 5

print("カメラスキャン中...")
found = []
for i in range(MAX_INDEX):
    cap = cv2.VideoCapture(i)
    if cap.isOpened():
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        print(f"  [{i}] {w}x{h} @ {fps:.0f}fps")
        found.append(i)
        cap.release()
    else:
        cap.release()

if not found:
    print("カメラが見つかりませんでした")
    print("WSL2の場合は usbipd-win でカメラを転送してください")
    print("  powershell: usbipd list  →  usbipd bind --busid <ID>  →  usbipd attach --wsl --busid <ID>")
else:
    print(f"\n使えるカメラ: インデックス {found}")
    print(f"アクセス起動コマンド例（インデックス {found[0]} を使う場合）:")
    print(f"  python pippi_client/access.py --detector mediapipe --camera {found[0]} --cam-url http://localhost:8000/api/camera/push --no-pose")
