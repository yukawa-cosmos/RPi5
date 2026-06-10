"""
人物検知モジュール

来訪者が近づいたことを検知してぴっぴの挨拶をトリガーする。

対応検知方式:
  - MockDetector      : テスト用（Enterキーで人が来たとみなす）
  - UltrasonicDetector: 超音波距離センサー HC-SR04（ラズパイ GPIO）
  - CameraDetector    : OpenCV 顔検知（PC / ラズパイカメラ）
  - PIRDetector       : 人感センサー（ラズパイ GPIO）
  - QRCodeDetector    : QRコード検知（PC / ラズパイカメラ）

使い方:
    from pippi_client.sensors.detector import build_detector
    detector = build_detector("mock")        # テスト
    detector = build_detector("ultrasonic")  # 実機（HC-SR04）
    detector = build_detector("camera")      # 実機（カメラ）
    detector = build_detector("qr")          # QRコード検知

    while True:
        detected = detector.wait_for_person()   # 人が来るまでブロック
        # QR の場合はデータを取り出せる
        if hasattr(detector, "last_qr_data") and detector.last_qr_data:
            print(detector.last_qr_data)
        # → ぴっぴ起動
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod


# ---------------------------------------------------------------------------
# 基底クラス
# ---------------------------------------------------------------------------

class PersonDetector(ABC):
    """人物検知の基底クラス。実装クラスはこれを継承する。"""

    @abstractmethod
    def wait_for_person(self, timeout: float | None = None) -> bool:
        """
        人が来るまでブロックする。

        Args:
            timeout: 秒数。None なら無制限に待つ。
        Returns:
            True  = 人を検知した
            False = タイムアウト
        """

    def cleanup(self) -> None:
        """GPIOや各種リソースを解放する（終了時に呼ぶ）"""

    def health_check(self) -> bool:
        """デバイスが正常に使えるか確認する（起動チェック用）"""
        return True


# ---------------------------------------------------------------------------
# MockDetector（テスト・開発用）
# ---------------------------------------------------------------------------

class MockDetector(PersonDetector):
    """
    テスト用検知器。
    Enter キー → 来訪者検知。
    APPT:ID または VISITOR:... を入力 → QRスキャンとして扱う。
    """

    def __init__(self):
        self.last_qr_data: str | None = None

    def wait_for_person(self, timeout: float | None = None) -> bool:
        print("\n[検知待機中] Enterで来訪者検知 / APPT:ID を入力でQRスキャンテストっぴ〜", flush=True)
        try:
            line = input().strip()
            if line.startswith("APPT:") or line.startswith("VISITOR:"):
                self.last_qr_data = line
                print(f"  → QRデータ受付: {line}", flush=True)
            else:
                self.last_qr_data = None
            return True
        except (EOFError, KeyboardInterrupt):
            return False


# ---------------------------------------------------------------------------
# UltrasonicDetector（HC-SR04 超音波距離センサー）
# ---------------------------------------------------------------------------

class UltrasonicDetector(PersonDetector):
    """
    HC-SR04 超音波距離センサーで人物を検知する。

    配線:
        VCC  → 5V
        GND  → GND
        TRIG → trigger_pin (BCM番号)
        ECHO → echo_pin    (BCM番号、5V→3.3V の分圧が必要)

    Args:
        trigger_pin     : TRIG ピン番号（BCM）デフォルト23
        echo_pin        : ECHO ピン番号（BCM）デフォルト24
        threshold_cm    : この距離以内に入ったら検知（デフォルト 150cm）
        check_interval  : 計測間隔（秒）デフォルト 0.3
    """

    def __init__(
        self,
        trigger_pin: int = 23,
        echo_pin: int = 24,
        threshold_cm: float = 150.0,
        check_interval: float = 0.3,
    ):
        try:
            import RPi.GPIO as GPIO
            self._GPIO = GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(trigger_pin, GPIO.OUT)
            GPIO.setup(echo_pin, GPIO.IN)
            GPIO.output(trigger_pin, False)
            time.sleep(0.5)
        except ImportError:
            raise RuntimeError(
                "RPi.GPIO が見つからないっぴ！ラズパイ上で実行してほしいっぴ！\n"
                "テスト環境では build_detector('mock') を使うっぴ〜"
            )
        self._trig = trigger_pin
        self._echo = echo_pin
        self._threshold = threshold_cm
        self._interval = check_interval

    def _measure_cm(self) -> float | None:
        """1回分の距離計測（cm）。失敗時は None を返す。"""
        GPIO = self._GPIO
        GPIO.output(self._trig, True)
        time.sleep(0.00001)
        GPIO.output(self._trig, False)

        start = time.time()
        timeout = start + 0.04  # 40ms タイムアウト

        while GPIO.input(self._echo) == 0:
            if time.time() > timeout:
                return None
        pulse_start = time.time()

        while GPIO.input(self._echo) == 1:
            if time.time() > timeout:
                return None
        pulse_end = time.time()

        return (pulse_end - pulse_start) * 17150  # 音速換算

    def wait_for_person(self, timeout: float | None = None) -> bool:
        deadline = time.time() + timeout if timeout else None
        print(f"[検知待機中] {self._threshold}cm 以内に入ると反応するっぴ〜", flush=True)
        while True:
            if deadline and time.time() > deadline:
                return False
            dist = self._measure_cm()
            if dist is not None and dist < self._threshold:
                print(f"  → 人を検知したっぴ！（{dist:.0f}cm）")
                return True
            time.sleep(self._interval)

    def cleanup(self) -> None:
        self._GPIO.cleanup()


# ---------------------------------------------------------------------------
# CameraDetector（OpenCV 顔検知 + 人体検知）
# ---------------------------------------------------------------------------

class CameraDetector(PersonDetector):
    """
    OpenCV で人物を検知する。顔検知（Haar Cascade）と人体検知（HOG）を
    組み合わせて精度を高める。

    検知フロー:
      1. フレームを取得して顔 or 人体を検出
      2. confirm_frames 連続フレームで検出されたら「人が来た」とみなす
      3. セッション終了後は wait_until_clear() で人が去るまで待機

    Args:
        camera_index    : カメラ番号（デフォルト 0）
        check_interval  : フレーム取得間隔（秒）デフォルト 0.3
        confirm_frames  : 連続検出が必要なフレーム数（誤検知防止）デフォルト 5
        clear_frames    : 「人が去った」とみなす連続未検出フレーム数 デフォルト 5
        use_hog         : 人体検知（HOG）も使う（遠距離に強いが重い）デフォルト True
        show_preview    : デバッグ用プレビュー表示 デフォルト False
    """

    def __init__(
        self,
        camera_index: int | str = 0,
        check_interval: float = 0.3,
        confirm_frames: int = 5,
        clear_frames: int = 5,
        use_hog: bool = True,
        show_preview: bool = False,
        shared_camera=None,
        motion_detect: bool = True,   # 動体検知プレフィルター（待機時の誤検知防止）
        motion_min_area: int = 1500,  # 動きとみなす最小変化ピクセル数
    ):
        self.recognized_visitor: dict | None = None  # 顔照合結果
        try:
            import cv2
            self._cv2 = cv2
        except ImportError:
            raise RuntimeError(
                "opencv-python が見つからないっぴ！\n"
                "pip install opencv-python-headless でインストールしてほしいっぴ！"
            )
        self._camera_index  = camera_index
        self._interval      = check_interval
        self._confirm_frames = confirm_frames
        self._clear_frames   = clear_frames
        self._use_hog        = use_hog
        self._show_preview   = show_preview
        self._shared_camera  = shared_camera
        self._db_available   = True  # DB接続失敗時にFalseにしてスキップ
        self._motion_detect  = motion_detect
        self._motion_min_area = motion_min_area
        # MOG2背景差分モデル（照明変化・カメラノイズを学習して吸収する）
        self._bg_subtractor  = (
            cv2.createBackgroundSubtractorMOG2(
                history=500, varThreshold=25, detectShadows=False
            ) if motion_detect else None
        )

        # 顔検知（Haar Cascade）
        self._face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        # 人体検知（HOG + SVM）
        if use_hog:
            self._hog = cv2.HOGDescriptor()
            self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        else:
            self._hog = None

        self._cap = None
        import threading as _threading
        self._stream_frame: "np.ndarray | None" = None
        self._stream_lock  = _threading.Lock()
        self._capture_stop = _threading.Event()
        self._capture_thread: "_threading.Thread | None" = None

    def _start_capture_thread(self) -> None:
        """カメラを開いて常時フレームをバッファに書き込むバックグラウンドスレッドを起動する。"""
        import threading
        if self._capture_thread is not None and self._capture_thread.is_alive():
            return
        self._open_camera()
        self._capture_stop.clear()

        def _loop():
            while not self._capture_stop.is_set():
                ret, frame = self._cap.read()
                if ret:
                    with self._stream_lock:
                        self._stream_frame = frame
                else:
                    time.sleep(0.05)

        self._capture_thread = threading.Thread(
            target=_loop, daemon=True, name="CameraDetector-capture"
        )
        self._capture_thread.start()
        # 最初のフレームが取れるまで待機（wait_for_person との競合防止）
        deadline = time.time() + 5.0
        while self._stream_frame is None and time.time() < deadline:
            time.sleep(0.05)

    def get_latest_frame(self):
        """最新フレームを返す。まだ1フレームも取れていなければ None。"""
        with self._stream_lock:
            return self._stream_frame.copy() if self._stream_frame is not None else None

    def health_check(self) -> bool:
        """カメラデバイスが実際に開けるか確認する"""
        try:
            self._open_camera()
            return self._cap is not None and self._cap.isOpened()
        except Exception:
            return False

    def _open_camera(self) -> None:
        if self._cap is None or not self._cap.isOpened():
            self._cap = self._cv2.VideoCapture(self._camera_index)
            if not self._cap.isOpened():
                raise RuntimeError(
                    f"カメラ {self._camera_index} を開けないっぴ！"
                    "接続を確認してほしいっぴ！"
                )

    def _detect_presence(self, frame) -> bool:
        """退出確認用：静止している人も検出できるよう閾値を緩めた判定。"""
        cv2 = self._cv2
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=3, minSize=(40, 40)
        )
        if len(faces) > 0:
            return True
        if self._hog is not None:
            found, weights = self._hog.detectMultiScale(
                frame, winStride=(8, 8), padding=(4, 4), scale=1.05
            )
            if any(w > 0.3 and r[2] * r[3] >= 3000 for r, w in zip(found, weights)):
                return True
        return False

    def _detect(self, frame) -> bool:
        """フレームに人がいるか判定する。顔 or 人体どちらかが反応したら True。"""
        cv2 = self._cv2
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # 顔検知 — minNeighbors=12 で背景パターンの誤検知を抑制
        # フレーム面積の3%未満（小さすぎる検出）は背景ノイズとして除外
        frame_area = frame.shape[0] * frame.shape[1]
        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=12, minSize=(100, 100)
        )
        real_faces = [(x, y, w, h) for (x, y, w, h) in faces
                      if w * h >= frame_area * 0.03]
        if real_faces:
            if self._show_preview:
                for (x, y, w, h) in real_faces:
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
                cv2.putText(frame, f"Face: {len(real_faces)}", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            return True

        # 人体検知（HOG）— 背景・家具の誤検知対策
        #   winStride=(16,16): スキャン間隔を広げて高速化 + 誤検知減
        #   scale=1.1: スケール変化を粗くして過検知を抑制
        #   weight > 0.7: SVMスコアが低い（自信のない）検出を除外
        #   w*h >= 10000: 端にちょっと映っただけの小さい検出を除外
        if self._hog is not None:
            found, weights = self._hog.detectMultiScale(
                frame, winStride=(16, 16), padding=(4, 4), scale=1.1
            )
            confident = [r for r, w in zip(found, weights)
                         if w > 0.7 and r[2] * r[3] >= 10000]
            if confident:
                if self._show_preview:
                    for (x, y, w, h) in confident:
                        cv2.rectangle(frame, (x, y), (x+w, y+h), (255, 165, 0), 2)
                    cv2.putText(frame, f"Body: {len(confident)}", (10, 60),
                                cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 165, 0), 2)
                return True

        return False

    def _next_frame(self):
        """フレームを1枚取得して返す。取得失敗時は None。"""
        if self._shared_camera is not None:
            return self._shared_camera.get_frame()
        # キャプチャスレッドが動いている場合はバッファから取得（二重読み取り防止）
        if self._capture_thread is not None and self._capture_thread.is_alive():
            return self.get_latest_frame()
        ret, frame = self._cap.read()
        if ret:
            with self._stream_lock:
                self._stream_frame = frame
        return frame if ret else None

    def _has_motion(self, frame) -> bool:
        """MOG2背景差分で動体ピクセル数を計算し、閾値超えかどうかを返す。
        照明変化・カメラ自動露出・センサーノイズは背景モデルが吸収する。"""
        mask = self._bg_subtractor.apply(frame)
        return int(self._cv2.countNonZero(mask)) >= self._motion_min_area

    def wait_for_person(self, timeout: float | None = None) -> bool:
        """
        人が連続 confirm_frames フレーム検出されるまで待機する。
        motion_detect=True のとき動体検知プレフィルターを通す：
        動きがないフレームはHaar/HOGを実行しないので静止した背景の誤検知がなくなる。
        """
        if self._shared_camera is None and not (
            self._capture_thread and self._capture_thread.is_alive()
        ):
            self._open_camera()
        deadline   = time.time() + timeout if timeout else None
        consecutive = 0
        print("[検知待機中] カメラで人を探しているっぴ〜", flush=True)

        while True:
            if deadline and time.time() > deadline:
                return False

            frame = self._next_frame()
            if frame is None:
                time.sleep(self._interval)
                consecutive = 0
                continue

            # --- 動体検知プレフィルター（MOG2背景差分）---
            if self._motion_detect:
                if not self._has_motion(frame):
                    # 動きなし → 照明変化・ノイズ・静止背景パターンはここで弾く
                    consecutive = 0
                    time.sleep(self._interval)
                    continue

            # --- 人物検知（動きが確認されたフレームのみ実行）---
            if self._detect(frame):
                consecutive += 1
                if consecutive >= self._confirm_frames:
                    print(f"  → 人を検知したっぴ！（{consecutive}フレーム連続）")
                    if self._show_preview and self._shared_camera is None:
                        self._cv2.destroyAllWindows()
                    self.recognized_visitor = self._try_recognize(frame)
                    return True
            else:
                consecutive = 0

            if self._show_preview and self._shared_camera is None:
                self._cv2.imshow("ぴっぴ カメラ", frame)
                if self._cv2.waitKey(1) & 0xFF == ord('q'):
                    return False

            time.sleep(self._interval)

    def _try_recognize(self, frame) -> "dict | None":
        """来訪者の顔照合はクラウド（GyomuSystem）側で行う設計のため、
        クライアント（ラズパイ）は検知のみを担当し、ここでは常に None を返す。
        ※スタッフ照合のみ access 側で staff_embeddings.json を使ってローカル実行する。"""
        return None

    def wait_until_clear(self, timeout: float = 30.0) -> None:
        """
        セッション終了後、人がフレームから去るまで待機する。
        これにより同じ人がいる間に次のセッションが起動しない。
        """
        if self._shared_camera is None and (self._cap is None or not self._cap.isOpened()):
            return
        deadline = time.time() + timeout
        consecutive_clear = 0
        print("[待機] 人が去るのを待っているっぴ〜", flush=True)

        while time.time() < deadline:
            frame = self._next_frame()
            if frame is None:
                time.sleep(self._interval)
                continue

            if self._detect(frame):
                consecutive_clear = 0
            else:
                consecutive_clear += 1
                if consecutive_clear >= self._clear_frames:
                    print("  → フレームがクリアになったっぴ！次の来訪者を待つっぴ〜")
                    return

            time.sleep(self._interval)

        print("  → タイムアウト。フレームクリア待ちを打ち切るっぴ〜")

    def cleanup(self) -> None:
        self._capture_stop.set()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=3)
        if self._cap and self._cap.isOpened():
            self._cap.release()
        if self._show_preview:
            self._cv2.destroyAllWindows()


# ---------------------------------------------------------------------------
# MediaPipeDetector（高速顔・人体検知）
# ---------------------------------------------------------------------------

class MediaPipeDetector(PersonDetector):
    """
    MediaPipe Tasks API (0.10.x) による高速顔・人体検知。

    Haar Cascade より大幅に高速・高精度。GPU不要でラズパイでも動作可能。
    モデルファイル（.tflite / .task）は初回起動時に自動ダウンロードして
    data/mediapipe_models/ にキャッシュする。

    検知フロー:
      1. BlazeFace で顔を検出（face_confidence 以上）
      2. 顔未検出なら PoseLandmarker で人体を検出（pose_confidence 以上）
      3. confirm_frames 連続フレームで検出されたら「人が来た」とみなす

    Args:
        camera_index     : カメラ番号（デフォルト 0）
        check_interval   : フレーム取得間隔（秒）デフォルト 0.1
        confirm_frames   : 連続検出が必要なフレーム数（デフォルト 3）
        clear_frames     : 「人が去った」とみなす連続未検出フレーム数（デフォルト 5）
        face_confidence  : 顔検出の信頼度閾値（デフォルト 0.6）
        pose_confidence  : Poseランドマーク可視度閾値（デフォルト 0.5）
        use_pose         : Poseによる人体検知も使う（デフォルト True）
        show_preview     : デバッグ用プレビュー表示（デフォルト False）
    """

    _MODEL_DIR = None  # クラス変数（遅延初期化）

    _FACE_MODEL_URL = (
        "https://storage.googleapis.com/mediapipe-models/"
        "face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
    )
    _POSE_MODEL_URL = (
        "https://storage.googleapis.com/mediapipe-models/"
        "pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
    )

    def __init__(
        self,
        camera_index: int | str = 0,
        check_interval: float = 0.1,
        confirm_frames: int = 5,
        clear_frames: int = 5,
        face_confidence: float = 0.72,
        pose_confidence: float = 0.5,
        use_pose: bool = True,
        show_preview: bool = False,
        shared_camera=None,
        motion_detect: bool = True,    # 動体検知プレフィルター（背景パターンの誤検知防止）
        motion_min_area: int = 1500,   # 動きとみなす最小変化ピクセル数
    ):
        try:
            import mediapipe as mp
            import cv2
            self._mp = mp
            self._cv2 = cv2
        except ImportError:
            raise RuntimeError(
                "mediapipe / opencv が見つからないっぴ！\n"
                "pip install mediapipe opencv-python-headless でインストールしてほしいっぴ！"
            )

        self._camera_index    = camera_index
        self._interval        = check_interval
        self._confirm_frames  = confirm_frames
        self._clear_frames    = clear_frames
        self._face_conf       = face_confidence
        self._pose_conf       = pose_confidence
        self._use_pose        = use_pose
        self._show_preview    = show_preview
        self._shared_camera   = shared_camera
        self._cap             = None
        self.recognized_visitor: dict | None = None
        self._motion_detect   = motion_detect
        self._motion_min_area = motion_min_area
        self._bg_subtractor   = (
            cv2.createBackgroundSubtractorMOG2(
                history=500, varThreshold=25, detectShadows=False
            ) if motion_detect else None
        )

        import threading as _threading
        self._stream_frame: "object | None" = None
        self._stream_lock   = _threading.Lock()
        self._capture_stop  = _threading.Event()
        self._capture_thread: "_threading.Thread | None" = None

        face_model = self._ensure_model("blaze_face_short_range.tflite", self._FACE_MODEL_URL)
        pose_model = self._ensure_model("pose_landmarker_lite.task", self._POSE_MODEL_URL) if use_pose else None

        import platform as _platform
        _is_wsl2 = (
            _platform.system() != "Windows"
            and os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")
        )

        if _is_wsl2:
            # WSL2: MediaPipe Tasks API が Python 3.12 でセグフォルトするため
            # TFLite を直接使用してBlazeFace推論を行う
            self._face_det = None
            self._pose = None
            self._tflite_interp = self._init_tflite(face_model, face_confidence)
            print("[MediaPipe] WSL2モード: TFLite直接推論で初期化完了", flush=True)
        else:
            try:
                from mediapipe.tasks import python as mp_tasks
                BaseOptions = mp_tasks.BaseOptions

                # GPU → CPU フォールバック
                for delegate in [BaseOptions.Delegate.GPU, BaseOptions.Delegate.CPU]:
                    try:
                        face_opts = mp_tasks.vision.FaceDetectorOptions(
                            base_options=BaseOptions(model_asset_path=face_model, delegate=delegate),
                            min_detection_confidence=face_confidence,
                        )
                        print("[MediaPipe] FaceDetector 初期化中...", flush=True)
                        self._face_det = mp_tasks.vision.FaceDetector.create_from_options(face_opts)
                        self._tflite_interp = None
                        print(f"[MediaPipe] FaceDetector 完了 ({delegate.name})", flush=True)
                        break
                    except (NotImplementedError, RuntimeError):
                        if delegate == BaseOptions.Delegate.CPU:
                            raise
                        print("[MediaPipe] GPU 不可、CPUモードで再試行...", flush=True)

                if use_pose and pose_model:
                    for delegate in [BaseOptions.Delegate.GPU, BaseOptions.Delegate.CPU]:
                        try:
                            pose_opts = mp_tasks.vision.PoseLandmarkerOptions(
                                base_options=BaseOptions(model_asset_path=pose_model, delegate=delegate),
                                running_mode=mp_tasks.vision.RunningMode.IMAGE,
                                min_pose_detection_confidence=0.5,
                                min_tracking_confidence=0.5,
                            )
                            print("[MediaPipe] PoseLandmarker 初期化中...", flush=True)
                            self._pose = mp_tasks.vision.PoseLandmarker.create_from_options(pose_opts)
                            print(f"[MediaPipe] PoseLandmarker 完了 ({delegate.name})", flush=True)
                            break
                        except (NotImplementedError, RuntimeError):
                            if delegate == BaseOptions.Delegate.CPU:
                                raise
                            print("[MediaPipe] GPU 不可、CPUモードで再試行...", flush=True)
                else:
                    self._pose = None
                    print("[MediaPipe] Pose スキップ", flush=True)

            except OSError as e:
                if "libGLESv2" in str(e):
                    raise RuntimeError(
                        "MediaPipe の初期化に失敗したっぴ！\n"
                        "  解決策: sudo apt-get install libgles2-mesa\n"
                    ) from e
                raise

    def _init_tflite(self, model_path: str, face_confidence: float):
        """WSL2用: TFLite直接推論の初期化。BlazeFaceアンカーを事前計算する。"""
        import warnings
        import numpy as np
        import tensorflow as tf

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            interp = tf.lite.Interpreter(model_path=model_path)
        interp.allocate_tensors()

        inp_details = interp.get_input_details()
        out_details = interp.get_output_details()
        self._tflite_input_idx = inp_details[0]["index"]

        # score / regressor 出力のインデックスを名前で特定
        self._tflite_score_idx = out_details[1]["index"]
        self._tflite_box_idx   = out_details[0]["index"]
        for d in out_details:
            name = d["name"].lower()
            if "classificat" in name:
                self._tflite_score_idx = d["index"]
            elif "regress" in name or "box" in name:
                self._tflite_box_idx = d["index"]

        # BlazeFace short-range (128x128) アンカー: 16x16x2 + 8x8x6 = 896
        # アンカーサイズ: 16x16グリッド=1/16, 8x8グリッド=1/8
        anchors = []
        anchor_sizes = []
        for y in range(16):
            for x in range(16):
                cx, cy = (x + 0.5) / 16.0, (y + 0.5) / 16.0
                anchors += [[cx, cy], [cx, cy]]
                anchor_sizes += [1.0 / 16.0, 1.0 / 16.0]
        for y in range(8):
            for x in range(8):
                cx, cy = (x + 0.5) / 8.0, (y + 0.5) / 8.0
                anchors += [[cx, cy]] * 6
                anchor_sizes += [1.0 / 8.0] * 6
        self._tflite_anchors      = np.array(anchors,      dtype=np.float32)
        self._tflite_anchor_sizes = np.array(anchor_sizes, dtype=np.float32)

        return interp

    def _detect_faces_tflite(self, frame, relaxed: bool = False) -> bool:
        """WSL2用: TFLite直接推論で顔を検出する。

        フィルタ条件（すべて満たす必要あり）:
          1. 最高スコアが _face_conf + 0.15（デフォルト≥0.87）以上
          2. _face_conf 以上のアンカーが 2 個以上（隣接ヒットで「サイズのある顔」を確認）
          3. 最高スコアのアンカーのデコード済み顔サイズが MIN_FACE_SIZE 以上

        relaxed=True（退出確認用）: 在席を取りこぼして誤って退出と判定しないよう
        閾値を大きく緩める。会話中に少し下を向く・横顔・近距離でも「居る」と判定する。
        （入室検知は relaxed=False の厳しい条件、退出確認は relaxed=True を使い分ける）
        """
        import numpy as np

        rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        resized = self._cv2.resize(rgb, (128, 128))
        inp = ((resized.astype(np.float32) / 127.5) - 1.0)[np.newaxis]

        self._tflite_interp.set_tensor(self._tflite_input_idx, inp)
        self._tflite_interp.invoke()

        raw_scores = self._tflite_interp.get_tensor(self._tflite_score_idx)[0, :, 0]
        scores = 1.0 / (1.0 + np.exp(-raw_scores.astype(np.float64)))

        if relaxed:
            high_conf     = max(self._face_conf - 0.22, 0.45)  # 既定 0.72 → 0.50
            min_anchors   = 1                                   # 隣接ヒット要件を撤廃
            min_face_size = 0.03                                # 顔が小さくても在席とみなす
        else:
            high_conf     = min(self._face_conf + 0.15, 0.95)
            min_anchors   = 2
            min_face_size = 0.07

        # 条件1: 最高スコアが高信頼閾値以上
        best_idx = int(np.argmax(scores))
        if scores[best_idx] < high_conf:
            return False

        # 条件2: 閾値以上のアンカーが min_anchors 個以上
        anchor_thresh = high_conf if relaxed else self._face_conf
        if np.sum(scores >= anchor_thresh) < min_anchors:
            return False

        # 条件3: デコードした顔の高さが min_face_size 以上
        MIN_FACE_SIZE = min_face_size
        try:
            raw_boxes = self._tflite_interp.get_tensor(self._tflite_box_idx)[0]
            anchor_size = float(self._tflite_anchor_sizes[best_idx])
            # BlazeFace box encoding: [dy, dx, log_h_ratio, log_w_ratio, ...]
            face_h = anchor_size * float(np.exp(np.clip(raw_boxes[best_idx, 2], -4, 4)))
            if face_h < MIN_FACE_SIZE:
                return False
        except Exception:
            pass  # デコード失敗時はサイズチェックをスキップ

        return True

    @classmethod
    def _ensure_model(cls, filename: str, url: str) -> str:
        """モデルファイルが data/mediapipe_models/ になければダウンロードして返す。"""
        import urllib.request
        from pathlib import Path
        if cls._MODEL_DIR is None:
            cls._MODEL_DIR = Path(__file__).parent.parent.parent / "data" / "mediapipe_models"
        cls._MODEL_DIR.mkdir(parents=True, exist_ok=True)
        path = cls._MODEL_DIR / filename
        if not path.exists():
            print(f"  [MediaPipe] {filename} をダウンロード中っぴ〜 ({url})", flush=True)
            urllib.request.urlretrieve(url, path)
            print(f"  [MediaPipe] ダウンロード完了っぴ！ → {path}", flush=True)
        return str(path)

    def _open_camera(self) -> None:
        if self._cap is None or not self._cap.isOpened():
            self._cap = self._cv2.VideoCapture(self._camera_index)
            if not self._cap.isOpened():
                raise RuntimeError(
                    f"カメラ {self._camera_index} を開けないっぴ！"
                )

    def _start_capture_thread(self) -> None:
        """CameraDetector と同じ常時フレームバッファ方式でカメラを起動する。
        _stream_camera がダッシュボードへフレームを送れるようになる。"""
        import threading
        if self._capture_thread is not None and self._capture_thread.is_alive():
            return
        self._open_camera()
        self._capture_stop.clear()

        def _loop():
            while not self._capture_stop.is_set():
                ret, frame = self._cap.read()
                if ret:
                    with self._stream_lock:
                        self._stream_frame = frame
                else:
                    time.sleep(0.05)

        self._capture_thread = threading.Thread(
            target=_loop, daemon=True, name="MediaPipeDetector-capture"
        )
        self._capture_thread.start()
        deadline = time.time() + 5.0
        while self._stream_frame is None and time.time() < deadline:
            time.sleep(0.05)

    def get_latest_frame(self):
        """最新フレームを返す。まだ取れていなければ None。"""
        with self._stream_lock:
            return self._stream_frame.copy() if self._stream_frame is not None else None

    def health_check(self) -> bool:
        try:
            self._open_camera()
            return self._cap is not None and self._cap.isOpened()
        except Exception:
            return False

    def _get_frame(self):
        if self._shared_camera is not None:
            return self._shared_camera.get_frame()
        if self._capture_thread is not None and self._capture_thread.is_alive():
            return self.get_latest_frame()
        ret, frame = self._cap.read()
        return frame if ret else None

    def _to_mp_image(self, frame):
        # RGB配列を変数に保持してGCによる早期解放を防ぐ
        rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        img._rgb_ref = rgb  # MediaPipeがC++側で参照している間、配列を生存させる
        return img

    def _detect(self, frame) -> bool:
        # WSL2: TFLite直接推論
        if self._tflite_interp is not None:
            return self._detect_faces_tflite(frame)

        try:
            mp_image = self._to_mp_image(frame)

            # BlazeFace — スコア0.85以上 かつ フレーム面積の2%以上の顔のみ採用
            face_result = self._face_det.detect(mp_image)
            if face_result.detections:
                frame_area = frame.shape[0] * frame.shape[1]
                for det in face_result.detections:
                    score = det.categories[0].score if det.categories else 0
                    bb = det.bounding_box
                    if score >= 0.85 and bb.width * bb.height >= frame_area * 0.02:
                        if self._show_preview:
                            self._cv2.rectangle(
                                frame,
                                (bb.origin_x, bb.origin_y),
                                (bb.origin_x + bb.width, bb.origin_y + bb.height),
                                (0, 255, 0), 2,
                            )
                        return True

            # PoseLandmarker（顔が映らない角度でも検知）
            if self._pose is not None:
                pose_result = self._pose.detect(mp_image)
                if pose_result.pose_landmarks:
                    key_ids = [0, 11, 12, 23, 24]  # 鼻・左右肩・左右腰
                    landmarks = pose_result.pose_landmarks[0]
                    if any(landmarks[i].visibility >= self._pose_conf for i in key_ids):
                        return True
        except OSError:
            pass  # Windows上でMediaPipeがアクセス違反を起こす既知問題

        return False

    def _detect_presence(self, frame) -> bool:
        """退出確認用：信頼度閾値を下げて静止した人も捕捉する。"""
        if self._tflite_interp is not None:
            return self._detect_faces_tflite(frame, relaxed=True)

        if self._face_det is None:
            return False

        try:
            mp_image = self._to_mp_image(frame)
            face_result = self._face_det.detect(mp_image)
            if face_result.detections:
                return True
            if self._pose is not None:
                pose_result = self._pose.detect(mp_image)
                if pose_result.pose_landmarks:
                    key_ids = [0, 11, 12, 23, 24]
                    landmarks = pose_result.pose_landmarks[0]
                    if any(landmarks[i].visibility >= 0.3 for i in key_ids):
                        return True
        except OSError:
            pass

        return False

    def _has_motion(self, frame) -> bool:
        """MOG2背景差分で動体ピクセル数を計算し、閾値超えかどうかを返す。"""
        mask = self._bg_subtractor.apply(frame)
        return int(self._cv2.countNonZero(mask)) >= self._motion_min_area

    def wait_for_person(self, timeout: float | None = None) -> bool:
        if self._shared_camera is None:
            self._open_camera()
        deadline    = time.time() + timeout if timeout else None
        consecutive = 0
        if not getattr(self, "_last_detected", False):
            print("[検知待機中] MediaPipeで人を探しているっぴ〜", flush=True)

        while True:
            if deadline and time.time() > deadline:
                self._last_detected = False
                return False

            frame = self._get_frame()
            if frame is None:
                time.sleep(self._interval)
                consecutive = 0
                continue

            # --- 動体検知プレフィルター ---
            # 動きがないフレームはMediaPipeにかけない → 静止した背景・ポスターを弾く
            if self._motion_detect and self._bg_subtractor is not None:
                if not self._has_motion(frame):
                    consecutive = 0
                    time.sleep(self._interval)
                    continue

            if self._detect(frame):
                consecutive += 1
                if consecutive >= self._confirm_frames:
                    if not getattr(self, "_last_detected", False):
                        print(f"  → 人を検知したっぴ！（MediaPipe / {consecutive}フレーム連続）")
                    self._last_detected = True
                    if self._show_preview:
                        self._cv2.destroyAllWindows()
                    return True
            else:
                consecutive = 0

            if self._show_preview:
                self._cv2.imshow("ぴっぴ MediaPipe", frame)
                if self._cv2.waitKey(1) & 0xFF == ord('q'):
                    return False

            time.sleep(self._interval)

    def wait_until_clear(self, timeout: float = 30.0) -> None:
        if self._shared_camera is None and (self._cap is None or not self._cap.isOpened()):
            return
        deadline          = time.time() + timeout
        consecutive_clear = 0
        print("[待機] 人が去るのを待っているっぴ〜", flush=True)

        while time.time() < deadline:
            frame = self._get_frame()
            if frame is None:
                time.sleep(self._interval)
                continue
            if self._detect(frame):
                consecutive_clear = 0
            else:
                consecutive_clear += 1
                if consecutive_clear >= self._clear_frames:
                    print("  → フレームがクリアになったっぴ！次の来訪者を待つっぴ〜")
                    return
            time.sleep(self._interval)

        print("  → タイムアウト。フレームクリア待ちを打ち切るっぴ〜")

    def cleanup(self) -> None:
        self._capture_stop.set()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=3)
        if self._face_det is not None:
            self._face_det.close()
        if self._pose is not None:
            self._pose.close()
        if self._cap and self._cap.isOpened():
            self._cap.release()
        if self._show_preview:
            self._cv2.destroyAllWindows()


# ---------------------------------------------------------------------------
# PIRDetector（人感センサー）
# ---------------------------------------------------------------------------

class PIRDetector(PersonDetector):
    """
    PIR（焦電型赤外線）人感センサーで検知する。

    Args:
        pin             : GPIO ピン番号（BCM）デフォルト 17
        check_interval  : ポーリング間隔（秒）デフォルト 0.2
    """

    def __init__(self, pin: int = 17, check_interval: float = 0.2):
        try:
            import RPi.GPIO as GPIO
            self._GPIO = GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(pin, GPIO.IN)
        except ImportError:
            raise RuntimeError(
                "RPi.GPIO が見つからないっぴ！ラズパイ上で実行してほしいっぴ！"
            )
        self._pin = pin
        self._interval = check_interval

    def wait_for_person(self, timeout: float | None = None) -> bool:
        deadline = time.time() + timeout if timeout else None
        print("[検知待機中] 人感センサーで待機中だっぴ〜", flush=True)
        while True:
            if deadline and time.time() > deadline:
                return False
            if self._GPIO.input(self._pin):
                print("  → 人感センサーが反応したっぴ！")
                return True
            time.sleep(self._interval)

    def cleanup(self) -> None:
        self._GPIO.cleanup()


# ---------------------------------------------------------------------------
# QRCodeDetector（QRコード検知）
# ---------------------------------------------------------------------------

class QRCodeDetector(PersonDetector):
    """
    QRコードを検知したら来訪者が来たとみなす。

    QRコードの中身が last_qr_data に保存されるので、
    ぴっぴのエンジンへの入力として使える。

    Args:
        camera_index : カメラ番号（デフォルト 0）
        timeout      : 1回の検知待機タイムアウト（秒）。0 で無制限。
        dedupe_sec   : 同一コードを再検出するまでの間隔（秒）
    """

    def __init__(
        self,
        camera_index: int = 0,
        timeout: float = 0,
        dedupe_sec: float = 3.0,
    ):
        from pippi_client.sensors.qr_reader import QRReader
        self._reader = QRReader(camera_index=camera_index)
        self._timeout = timeout
        self._dedupe_sec = dedupe_sec
        self.last_qr_data: str | None = None  # 最後に読んだQRデータ

    def wait_for_person(self, timeout: float | None = None) -> bool:
        """
        QRコードが検知されるまでブロックする。
        検知したら last_qr_data にデータを格納して True を返す。
        """
        t = timeout if timeout is not None else self._timeout
        print("[検知待機中] QRコードをカメラに向けてほしいっぴ〜", flush=True)
        data = self._reader.read_from_camera(
            timeout=t,
            dedupe_sec=self._dedupe_sec,
        )
        if data:
            self.last_qr_data = data
            print(f"  → QRコードを検知したっぴ！: {data[:60]}")
            return True
        self.last_qr_data = None
        return False

    def cleanup(self) -> None:
        self._reader.close()


# ---------------------------------------------------------------------------
# ファクトリ関数
# ---------------------------------------------------------------------------

def build_detector(mode: str = "mock", **kwargs) -> PersonDetector:
    """
    検知方式を文字列で指定してDetectorを生成する。

    Args:
        mode: "mock" / "ultrasonic" / "camera" / "pir" / "qr"
        **kwargs: 各Detectorのコンストラクタに渡す引数

    例:
        build_detector("mock")
        build_detector("ultrasonic", threshold_cm=120)
        build_detector("camera", camera_index=0)
        build_detector("pir", pin=17)
        build_detector("qr", camera_index=0)
    """
    mode = mode.lower()
    if mode == "mock":
        return MockDetector(**kwargs)
    elif mode == "ultrasonic":
        return UltrasonicDetector(**kwargs)
    elif mode == "camera":
        return CameraDetector(**kwargs)
    elif mode == "mediapipe":
        return MediaPipeDetector(**kwargs)
    elif mode == "pir":
        return PIRDetector(**kwargs)
    elif mode == "qr":
        return QRCodeDetector(**kwargs)
    else:
        raise ValueError(
            f"未対応の検知モードだっぴ: {mode}（mock/ultrasonic/camera/mediapipe/pir/qr から選んでほしいっぴ）"
        )
