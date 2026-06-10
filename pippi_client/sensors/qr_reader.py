"""
QRコード読み取りモジュール

PC（Windows/Linux）・ラズパイ共通で動作する。
利用可能なライブラリを自動検出し、精度の高い順に試みる。

検出パイプライン（上から順に試す）:
  1. WeChat QR Detector  … 斜め・小・ぼやけに強い（DLベース）
  2. pyzbar              … 汎用・回転に強い
  3. cv2.QRCodeDetector  … 常に使えるフォールバック
  ※ 各ライブラリで見つからなければ前処理・マルチスケールで再挑戦

依存:
    pip install opencv-python            # PC（ウィンドウあり）
    pip install opencv-contrib-python    # WeChat QR を使う場合（opencv-pythonと排他）
    pip install opencv-python-headless   # ラズパイ（ウィンドウなし）
    pip install pyzbar                   # 任意（精度向上）
    sudo apt install libzbar0            # pyzbar のシステムライブラリ（Pi/Linux）

使い方:
    reader = QRReader()
    reader.print_backends()   # 使用中のライブラリを確認

    # 1ショット
    data = reader.read_from_camera(timeout=30.0)
    print(data)   # → "https://example.com" など

    # 連続読み取り（Ctrl+C で停止）
    for qr in reader.read_continuous(camera_index=0):
        print(f"検知: {qr}")
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Generator


@dataclass
class QRResult:
    """QR読み取り結果"""
    data: str               # デコード済みテキスト
    points: list | None     # 検出枠の4頂点座標（None の場合もある）


class QRReader:
    """
    カメラからQRコードを読み取るクラス。

    Args:
        camera_index : カメラ番号（デフォルト 0）
        use_pyzbar   : pyzbar を使うか（デフォルト True）
        use_wechat   : WeChat QR Detector を使うか（デフォルト True）
        preprocess   : 前処理バリアントで再試行するか（デフォルト True）
        multiscale   : 拡大スケールで再試行するか（デフォルト True）
        interval     : フレーム間隔（秒）。None = キャプチャ速度に任せる
    """

    def __init__(
        self,
        camera_index: int = 0,
        use_pyzbar: bool = True,
        use_wechat: bool = True,
        preprocess: bool = True,
        multiscale: bool = True,
        interval: float | None = 0.05,
    ):
        try:
            import cv2
            self._cv2 = cv2
        except ImportError:
            raise RuntimeError(
                "opencv-python が見つからないっぴ！\n"
                "pip install opencv-python でインストールしてほしいっぴ！"
            )

        self._camera_index = camera_index
        self._interval = interval
        self._preprocess = preprocess
        self._multiscale = multiscale
        self._cap = None

        # --- ライブラリ初期化（優先度順）---

        # 1. WeChat QR Detector（opencv-contrib-python が必要）
        self._wechat = None
        if use_wechat:
            try:
                self._wechat = cv2.wechat_qrcode_WeChatQRCode()
            except AttributeError:
                pass  # opencv-contrib-python 未インストール

        # 2. pyzbar
        self._pyzbar = None
        if use_pyzbar:
            try:
                import pyzbar.pyzbar as pyzbar
                self._pyzbar = pyzbar
            except ImportError:
                pass

        # 3. OpenCV 組み込み QR デコーダ（常に使える）
        self._qr_detector = cv2.QRCodeDetector()

    def print_backends(self) -> None:
        """使用中のバックエンドを表示する"""
        print("QRReader backends:")
        print(f"  WeChat QR : {'OK (最高精度)' if self._wechat   else '-- (pip install opencv-contrib-python)'}")
        print(f"  pyzbar    : {'OK (高精度)'   if self._pyzbar   else '-- (pip install pyzbar)'}")
        print(f"  OpenCV QR : OK (標準)'")
        print(f"  前処理    : {'有効' if self._preprocess else '無効'}")
        print(f"  マルチスケール: {'有効' if self._multiscale else '無効'}")

    # ------------------------------------------------------------------
    # カメラ管理
    # ------------------------------------------------------------------

    def open(self) -> None:
        """カメラを開く"""
        if self._cap is None or not self._cap.isOpened():
            cv2 = self._cv2
            import os as _os
            _fd = _os.dup(2)
            try:
                _null = _os.open(_os.devnull, _os.O_WRONLY)
                _os.dup2(_null, 2)
                _os.close(_null)
                self._cap = cv2.VideoCapture(self._camera_index)
            finally:
                _os.dup2(_fd, 2)
                _os.close(_fd)
            if not self._cap.isOpened():
                raise RuntimeError(
                    f"カメラ {self._camera_index} を開けなかったっぴ！\n"
                    "カメラが接続されているか確認してほしいっぴ〜"
                )
            # バッファを減らしてリアルタイム性を上げる
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def close(self) -> None:
        """カメラを閉じる"""
        if self._cap and self._cap.isOpened():
            self._cap.release()
            self._cap = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # 前処理
    # ------------------------------------------------------------------

    def _preprocess_variants(self, frame) -> list:
        """
        1フレームから複数の前処理バリアントを生成して返す。
        標準検出で失敗した場合に再試行する用。

        Returns:
            前処理済みフレームのリスト（BGRまたはグレースケール）
        """
        import numpy as np
        cv2 = self._cv2
        variants = []

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # 1. CLAHE（コントラスト均一化）― 暗い・逆光環境
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        variants.append(clahe.apply(gray))

        # 2. シャープ化 ― ぼやけた・低解像度
        kernel = np.array([[0, -1, 0],
                           [-1,  5, -1],
                           [0, -1, 0]], dtype=np.float32)
        sharpened = cv2.filter2D(gray, -1, kernel)
        variants.append(sharpened)

        # 3. 適応的二値化 ― 照明ムラ・グラデーション背景
        thresh = cv2.adaptiveThreshold(
            gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 11, 2,
        )
        variants.append(thresh)

        # 4. ガンマ補正（明るくする）― 暗い環境
        lut = np.array([((i / 255.0) ** 0.5) * 255 for i in range(256)], dtype=np.uint8)
        variants.append(lut[gray])

        return variants

    def _multiscale_variants(self, frame) -> list:
        """
        フレームを拡大したバリアントを生成する。
        小さいQR・遠いQRに効果的。
        """
        cv2 = self._cv2
        h, w = frame.shape[:2]
        variants = []
        for scale in (1.5, 2.0):
            resized = cv2.resize(frame, (int(w * scale), int(h * scale)),
                                 interpolation=cv2.INTER_CUBIC)
            variants.append(resized)
        return variants

    # ------------------------------------------------------------------
    # フレーム解析
    # ------------------------------------------------------------------

    def _decode_single(self, img) -> list[QRResult]:
        """
        1枚の画像（BGR またはグレースケール）にデコードを試みる。
        利用可能なバックエンドを優先度順に試す。
        """
        cv2 = self._cv2
        results: list[QRResult] = []

        # BGR かグレースケールかを判定
        is_gray = (img.ndim == 2)
        bgr  = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if is_gray else img
        gray = img if is_gray else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # --- 1. WeChat QR Detector ---
        if self._wechat is not None:
            try:
                texts, pts_list = self._wechat.detectAndDecode(bgr)
                for i, text in enumerate(texts):
                    if text:
                        pts = pts_list[i].tolist() if pts_list is not None else None
                        results.append(QRResult(data=text, points=pts))
                if results:
                    return results
            except Exception:
                pass

        # --- 2. pyzbar ---
        if self._pyzbar is not None:
            for obj in self._pyzbar.decode(bgr):
                data = obj.data.decode("utf-8", errors="replace")
                pts = [[p.x, p.y] for p in obj.polygon]
                results.append(QRResult(data=data, points=pts))
            if results:
                return results

        # --- 3. OpenCV QRCodeDetector ---
        data, points, _ = self._qr_detector.detectAndDecode(gray)
        if data:
            pts = points[0].tolist() if points is not None else None
            results.append(QRResult(data=data, points=pts))

        return results

    def decode_frame(self, frame) -> list[QRResult]:
        """
        1フレームから全QRコードを検出してリストで返す。
        見つからない場合は前処理・マルチスケールで再挑戦する。

        Returns:
            QRResult のリスト（検出なしなら空リスト）
        """
        # まず元フレームで試す
        results = self._decode_single(frame)
        if results:
            return results

        # 前処理バリアント
        if self._preprocess:
            for variant in self._preprocess_variants(frame):
                results = self._decode_single(variant)
                if results:
                    return results

        # マルチスケール（拡大）
        if self._multiscale:
            for scaled in self._multiscale_variants(frame):
                results = self._decode_single(scaled)
                if results:
                    return results

        return []

    # ------------------------------------------------------------------
    # 高レベルAPI
    # ------------------------------------------------------------------

    def read_from_camera(
        self,
        timeout: float = 30.0,
        dedupe_sec: float = 2.0,
        shared_camera=None,
    ) -> str | None:
        """
        カメラを開いてQRコードが検出されるまで待ち、最初のデータを返す。

        Args:
            timeout       : 最大待機秒数（0 以下で無制限）
            dedupe_sec    : 同一コードを何秒間無視するか
            shared_camera : SharedCamera インスタンス（あれば自前でカメラを開かない）
        """
        if shared_camera is None:
            self.open()
        deadline = time.time() + timeout if timeout > 0 else None
        last_seen: dict[str, float] = {}

        try:
            while True:
                if deadline and time.time() > deadline:
                    return None

                if shared_camera is not None:
                    frame = shared_camera.get_frame()
                    if frame is None:
                        time.sleep(0.05)
                        continue
                else:
                    ret, frame = self._cap.read()
                    if not ret:
                        time.sleep(0.1)
                        continue

                for r in self.decode_frame(frame):
                    now = time.time()
                    if now - last_seen.get(r.data, 0) > dedupe_sec:
                        last_seen[r.data] = now
                        return r.data

                if self._interval:
                    time.sleep(self._interval)
        finally:
            if shared_camera is None:
                self.close()

    def read_continuous(
        self,
        dedupe_sec: float = 2.0,
        shared_camera=None,
        poll_interval: float | None = None,
    ) -> Generator[QRResult, None, None]:
        """
        カメラを開き続け、QRコードを検出するたびに QRResult を yield する。

        Args:
            dedupe_sec    : 同一コードを再検出するまでの間隔（秒）
            shared_camera : SharedCamera インスタンス（あれば自前でカメラを開かない）
            poll_interval : shared_camera 使用時のポーリング間隔（None で self._interval）

        使い方:
            reader = QRReader()
            for result in reader.read_continuous():
                print(result.data)
        """
        if shared_camera is None:
            self.open()
        last_seen: dict[str, float] = {}
        interval = poll_interval if poll_interval is not None else self._interval

        try:
            while True:
                if shared_camera is not None:
                    frame = shared_camera.get_frame()
                    if frame is None:
                        time.sleep(0.05)
                        continue
                else:
                    ret, frame = self._cap.read()
                    if not ret:
                        time.sleep(0.1)
                        continue

                for r in self.decode_frame(frame):
                    now = time.time()
                    if now - last_seen.get(r.data, 0) > dedupe_sec:
                        last_seen[r.data] = now
                        yield r

                if interval:
                    time.sleep(interval)
        finally:
            if shared_camera is None:
                self.close()

    def read_with_preview(
        self,
        window_title: str = "QRコード読み取り  [Q: quit]",
        dedupe_sec: float = 2.0,
        detect_interval: float = 0.1,
    ) -> Generator[QRResult, None, None]:
        """
        カメラプレビューウィンドウを表示しながらQRを読み取るジェネレータ。
        PC（ヘッドあり環境）専用。ラズパイでは read_continuous() を使うこと。

        表示はフルFPS、QR検出は別スレッドで detect_interval 秒ごとに実行。
        QRが検出されるたびに QRResult を yield する。
        Qキー / Esc / ウィンドウを閉じると終了。

        Args:
            detect_interval : 検出スレッドの処理間隔（秒）。
                              小さいほど反応が早いが CPU 負荷が増える。
        """
        import threading
        import queue

        cv2 = self._cv2
        self.open()

        # スレッド間共有
        latest_frame: list = [None]       # 検出スレッドが読むフレーム
        frame_lock = threading.Lock()
        result_queue: queue.Queue = queue.Queue()
        stop_event = threading.Event()
        last_seen: dict[str, float] = {}

        # --- 検出スレッド ---
        def _detect_worker():
            while not stop_event.is_set():
                with frame_lock:
                    frame = latest_frame[0]
                if frame is None:
                    time.sleep(0.01)
                    continue

                for r in self.decode_frame(frame):
                    now = time.time()
                    if now - last_seen.get(r.data, 0) > dedupe_sec:
                        last_seen[r.data] = now
                        result_queue.put(r)

                time.sleep(detect_interval)

        detect_thread = threading.Thread(target=_detect_worker, daemon=True)
        detect_thread.start()

        # --- 表示スレッド（メインスレッド）---
        overlay_results: list[QRResult] = []  # 枠描画用（最後の検出結果）
        overlay_expire = 0.0                   # 枠の表示期限

        try:
            while True:
                ret, frame = self._cap.read()
                if not ret:
                    continue

                # 検出スレッドにフレームを渡す
                with frame_lock:
                    latest_frame[0] = frame.copy()

                # 検出結果キューを確認
                new_results = []
                while not result_queue.empty():
                    r = result_queue.get_nowait()
                    new_results.append(r)
                    overlay_results = [r]   # 枠表示を更新
                    overlay_expire = time.time() + 2.0

                # オーバーレイ描画
                display = frame.copy()
                if time.time() < overlay_expire:
                    for r in overlay_results:
                        if r.points:
                            pts = [list(map(int, p)) for p in r.points]
                            for i in range(len(pts)):
                                cv2.line(
                                    display,
                                    tuple(pts[i]),
                                    tuple(pts[(i + 1) % len(pts)]),
                                    (0, 255, 0), 3,
                                )
                            x = min(p[0] for p in pts)
                            y = min(p[1] for p in pts) - 12
                            label = r.data if len(r.data) <= 40 else r.data[:37] + "..."
                            cv2.putText(
                                display, label,
                                (x, max(y, 20)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.65,
                                (0, 255, 0), 2,
                            )

                cv2.imshow(window_title, display)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), ord("Q"), 27):
                    break
                # ウィンドウが閉じられた場合
                if cv2.getWindowProperty(window_title, cv2.WND_PROP_VISIBLE) < 1:
                    break

                for r in new_results:
                    yield r

        finally:
            stop_event.set()
            detect_thread.join(timeout=1.0)
            self.close()
            cv2.destroyAllWindows()
