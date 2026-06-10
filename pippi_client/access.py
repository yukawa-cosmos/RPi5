"""
ぴっぴ Access（ラズパイ側）

センサー検知 → マイク録音 → GyomuSystem(クラウド)へWebSocketで送信
クラウドから音声データを受信 → スピーカー再生 → モーター制御

使い方（client/ ディレクトリで実行。本番は systemd の pippi-robot 経由で自動起動）:
    python -m pippi_client.access
    python -m pippi_client.access --detector pir
    python -m pippi_client.access --detector qr

オプション:
    --detector   検知方式（mock / ultrasonic / camera / mediapipe / pir / qr）デフォルト: mock
    --threshold  超音波センサー検知距離cm（ultrasonic時のみ）
    --camera     カメラ番号（camera/qr時のみ）
    --gyomu-url  GyomuSystem WebSocket URL（デフォルト: ws://localhost:8001/ws/robot）
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import threading

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# MediaPipe/TensorFlow のノイズログを抑制（C++がfd2に直接書くのでfd2ごとリダイレクト）
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
import logging
logging.getLogger("tensorflow").setLevel(logging.ERROR)

_real_stderr_fd = os.dup(2)                              # 元のstderrを退避
_devnull_fd = os.open(os.devnull, os.O_WRONLY)
os.dup2(_devnull_fd, 2)                                  # fd2→devnull（C++ノイズ消去）
os.close(_devnull_fd)
sys.stderr = os.fdopen(_real_stderr_fd, "w", buffering=1)  # Python出力は元に戻す

import numpy as np
import sounddevice as sd
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed as WsConnectionClosed

# client/ ディレクトリを import パスに追加（pippi_client パッケージを読み込むため）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pippi_client.sensors.detector import build_detector
from pippi_client.audio import AudioPlayer

# ---------------------------------------------------------------------------
# 音声定数
# ---------------------------------------------------------------------------

SAMPLE_RATE   = 16000
CHANNELS      = 1
RECORD_SECS   = 8.0      # 一発話あたりの最大録音秒数
DTYPE         = "int16"

# ---------------------------------------------------------------------------
# 音声入出力
# ---------------------------------------------------------------------------

_VAD_CHUNK          = 512    # 約32ms/チャンク
_VAD_THRESHOLD      = 350    # 動的閾値の最低値（環境が静かな場合のフロア）
_VAD_NOISE_MULT     = 3.5    # 動的閾値 = 推定ノイズフロア × この倍率
_VAD_CONFIRM        = 4      # 連続N回閾値超えで発話確定（~130ms）一瞬のノイズを除外
_VAD_SILENCE        = 1.0    # 無音がこの秒数続いたら録音終了
_VAD_PRE_BUF        = 0.5    # 発話前プリバッファ（語頭が切れないよう）
_VAD_POLL_SECS      = 15.0   # 1ポーリングあたりの最大秒数（安全弁）
_VAD_MIN_SPEECH_RMS = 350    # 発話部分の最低RMS（これ未満は雑音とみなして破棄）


def _capture_once(device: int | None, stop_event: threading.Event | None = None,
                  seed_buffer: list | None = None) -> bytes | None:
    """1回のVAD録音。
    ・プリバッファから環境音を推定し動的閾値を設定（環境音の3倍）
    ・連続4チャンク閾値超えで発話確定（一瞬のノイズスパイクを無視）
    ・seed_buffer: ウォームマイクが事前収集したチャンクを初期プリバッファとして注入できる
    発話検知→PCM、タイムアウト→None
    """
    try:
        if device is not None:
            dev_info = sd.query_devices(device, "input")
        else:
            dev_info = sd.query_devices(kind="input")
        native_rate = int(dev_info["default_samplerate"])

        silence_limit = int(_VAD_SILENCE  * native_rate / _VAD_CHUNK)
        pre_buf_limit = int(_VAD_PRE_BUF  * native_rate / _VAD_CHUNK)
        max_chunks    = int(_VAD_POLL_SECS * native_rate / _VAD_CHUNK)

        frames: list      = []
        pre_buffer: list  = list(seed_buffer) if seed_buffer else []
        if len(pre_buffer) > pre_buf_limit + _VAD_CONFIRM:
            pre_buffer = pre_buffer[-(pre_buf_limit + _VAD_CONFIRM):]
        confirm_buf: list = []   # 発話確定前の候補チャンク
        speaking          = False
        silence_count     = 0
        active_threshold  = _VAD_THRESHOLD
        speech_start_idx  = 0   # frames 内で発話が始まるインデックス

        with sd.InputStream(samplerate=native_rate, channels=CHANNELS,
                            dtype=DTYPE, blocksize=_VAD_CHUNK, device=device) as stream:
            for i in range(max_chunks):
                if stop_event and stop_event.is_set():
                    return None
                chunk, _ = stream.read(_VAD_CHUNK)
                rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))

                if not speaking:
                    # 20チャンク（約650ms）ごとにプリバッファから環境音を推定して閾値を更新
                    if i % 20 == 0 and len(pre_buffer) >= 4:
                        _recent = np.concatenate([c.flatten() for c in pre_buffer[-8:]])
                        _noise  = float(np.sqrt(np.mean(_recent.astype(np.float32) ** 2)))
                        active_threshold = max(_VAD_THRESHOLD, _noise * _VAD_NOISE_MULT)

                    if rms >= active_threshold:
                        confirm_buf.append(chunk.copy())
                        if len(confirm_buf) >= _VAD_CONFIRM:
                            # 連続N回超え → 発話確定
                            speaking = True
                            speech_start_idx = len(pre_buffer)
                            frames.extend(pre_buffer)
                            frames.extend(confirm_buf)
                            confirm_buf = []
                            print(f"  [Access] 発話検知（閾値:{active_threshold:.0f}）...", flush=True)
                    else:
                        # 閾値以下 → 確認バッファをプリバッファに戻してリセット
                        if confirm_buf:
                            pre_buffer.extend(confirm_buf)
                            confirm_buf = []
                        pre_buffer.append(chunk.copy())
                        if len(pre_buffer) > pre_buf_limit + _VAD_CONFIRM:
                            pre_buffer.pop(0)
                else:
                    frames.append(chunk.copy())
                    if rms < active_threshold:
                        silence_count += 1
                        if silence_count >= silence_limit:
                            break
                    else:
                        silence_count = 0

        if not frames:
            return None

        # 発話部分（プリバッファを除く）の RMS が低すぎる場合は雑音とみなして破棄
        speech_frames = frames[speech_start_idx:]
        if speech_frames:
            _speech_arr = np.concatenate([c.flatten() for c in speech_frames]).astype(np.float32)
            _speech_rms = float(np.sqrt(np.mean(_speech_arr ** 2)))
            if _speech_rms < _VAD_MIN_SPEECH_RMS:
                print(f"  [Access] RMS低すぎ ({_speech_rms:.0f} < {_VAD_MIN_SPEECH_RMS}) → 雑音破棄", flush=True)
                return None

        audio = np.concatenate(frames, axis=0).flatten().astype(np.float32)
        if native_rate != SAMPLE_RATE:
            n_out = int(len(audio) * SAMPLE_RATE / native_rate)
            audio = np.interp(
                np.linspace(0, len(audio) - 1, n_out),
                np.arange(len(audio)),
                audio,
            )
        return audio.astype(np.int16).tobytes()

    except Exception as e:
        print(f"  [Access] 音声入力エラー（{e}）")
        return None


def capture_audio(device: int | None = None,
                  stop_event: threading.Event | None = None,
                  max_polls: int | None = None) -> bytes | None:
    """発話を検知するまで待機し、発話終了後のPCMを返す。
    max_polls: None=無制限、N=N回ポーリング後に None を返す（来訪者が黙っている）。
    戻り値が None = 無音タイムアウト（STT失敗とは区別する）。
    """
    import time as _time
    _fast_fail = 0  # _VAD_POLL_SECS より早く終了した連続回数（デバイスエラー検出用）
    _polls = 0
    while True:
        if stop_event and stop_event.is_set():
            return bytes(int(RECORD_SECS * SAMPLE_RATE) * 2)
        _t0 = _time.monotonic()
        result = _capture_once(device, stop_event=stop_event)
        _elapsed = _time.monotonic() - _t0
        if result is not None:
            return result
        _polls += 1
        if max_polls is not None and _polls >= max_polls:
            print(f"  [Access] {max_polls}回無音タイムアウト → 無音通知", flush=True)
            return None  # 無音タイムアウト（STT失敗とは別扱い）
        # _VAD_POLL_SECS（15秒）より大幅に短い場合 = デバイスエラーによる即時失敗
        if _elapsed < 2.0:
            _fast_fail += 1
            if _fast_fail >= 3:
                print("  [Access] 音声デバイスに連続エラー。--mock-audio で起動するか音声デバイスを確認してほしいっぴ", flush=True)
                _time.sleep(3.0)  # CPU を使い切らないよう間を空ける
        else:
            _fast_fail = 0  # 正常タイムアウトはリセット


def capture_audio_or_qr(
    device: int | None,
    cap,
    cv2_mod,
    person_detector=None,
    seed_buffer: list | None = None,
    overall_timeout: float = 120.0,
) -> tuple[bytes | None, str | None, bool]:
    """音声・QRコード・来訪者退出を並列監視し、最初に検知したものを返す。
    Returns: (pcm_bytes, qr_string, visitor_left)
    overall_timeout: 発話・QR・退出のいずれも検知しないまま経過したら打ち切る秒数。
        カメラ故障や恒常的な誤検出で退出検知が永久に成立しないケースの最終保険。
        打ち切り時は (None, None, False) を返し、呼び出し側が silence_timeout を送る。
    """
    import time as _time
    _done          = threading.Event()
    _pcm:  list[bytes | None] = [None]
    _qr:   list[str | None]   = [None]
    _left: list[bool]         = [False]
    _latest_frame: list       = [None]
    _frame_lock = threading.Lock()
    _qr_visible: list[bool]   = [False]  # QRコードが画面内に見えているフラグ

    # detector がフレーム取得APIを持つ場合は cap.read() を直接呼ばない。
    # detector のキャプチャスレッドが同一 VideoCapture を read しているため、
    # ここで生 cap.read() すると同じデバイスを2スレッドから同時 read してしまい
    # （OpenCV の VideoCapture は非スレッドセーフ）フレーム破損・取りこぼしで
    # 退出検知が不安定化する。detector._get_frame() 経由に統一する。
    _use_detector_frame = person_detector is not None and hasattr(person_detector, "_get_frame")

    def _frame_reader():
        """カメラフレームを定期取得し、QR/presence ワーカーと共有する。"""
        while not _done.is_set():
            frame = None
            if _use_detector_frame:
                try:
                    frame = person_detector._get_frame()
                except Exception:
                    frame = None
            else:
                ret, f = cap.read()
                frame = f if ret else None
            if frame is not None:
                with _frame_lock:
                    _latest_frame[0] = frame
            _time.sleep(0.05)

    def _audio_worker():
        result = _capture_once(device, stop_event=_done, seed_buffer=seed_buffer) \
                 or capture_audio(device=device, stop_event=_done)
        if not _done.is_set():
            _pcm[0] = result
            _done.set()

    def _qr_worker():
        qr_det = cv2_mod.QRCodeDetector()
        while not _done.is_set():
            with _frame_lock:
                frame = _latest_frame[0]
            if frame is not None:
                try:
                    data, bbox, _ = qr_det.detectAndDecode(frame)
                except Exception:
                    data, bbox = "", None
                _qr_visible[0] = bbox is not None  # QR枠が検出されている間はフラグON
                if data and not _done.is_set():
                    try:
                        import winsound
                        winsound.Beep(1800, 80)
                        winsound.Beep(1800, 80)
                    except Exception:
                        pass
                    _qr[0] = data
                    _done.set()
                    return
            _time.sleep(0.15)

    def _presence_worker():
        """人物が画角から消えたら退出とみなす（5秒連続で未検出 + 最初の2秒は猶予）。
        単発の在席検出では absent をリセットせず、2回連続で在席を確認したときだけリセットする。
        緩めた _detect_presence の偶発的な誤検出で退出カウントが毎回リセットされ、
        来訪者退出検知から抜け出せなくなる（セッションが終わらない）問題を防ぐ。"""
        if person_detector is None or not hasattr(person_detector, "_detect"):
            return
        absent = 0
        present_streak = 0
        LIMIT  = 17  # 17 × 0.3s ≈ 5秒（身体を傾けたりしても誤検出しにくい閾値）
        GRACE  = 7   # 最初の 7 × 0.3s ≈ 2秒 は退出判定しない（会話開始直後の誤検出防止）
        PRESENT_CONFIRM = 2  # 2回連続で在席を確認したときだけ absent をリセット（単発の誤検出を無視）
        elapsed = 0
        while not _done.is_set():
            with _frame_lock:
                frame = _latest_frame[0]
            if frame is not None:
                elapsed += 1
                # QRコードが画面内にある間は退出カウントをリセット
                if _qr_visible[0]:
                    present_streak = 0
                    absent = 0
                elif person_detector._detect_presence(frame):
                    present_streak += 1
                    if present_streak >= PRESENT_CONFIRM:
                        absent = 0
                else:
                    present_streak = 0
                    if elapsed > GRACE:
                        absent += 1
                        if absent >= LIMIT and not _done.is_set():
                            print("  [Access] 来訪者退出検知（5秒間不在）", flush=True)
                            _left[0] = True
                            _done.set()
                            return
            _time.sleep(0.3)

    def _timeout_worker():
        """最終保険：一定時間 何も検知しなければ打ち切る。
        退出検知（カメラ）が唯一の終了経路のため、カメラ故障や恒常的な誤検出で
        退出が永久に成立しないとセッションが終わらなくなる。これを時間で打ち切る。"""
        if overall_timeout is None or overall_timeout <= 0:
            return
        waited = 0.0
        while not _done.is_set():
            _time.sleep(0.5)
            waited += 0.5
            if waited >= overall_timeout:
                if not _done.is_set():
                    print(f"  [Access] 録音タイムアウト（{int(overall_timeout)}秒 無入力）→ 打ち切り", flush=True)
                    _done.set()
                return

    threading.Thread(target=_audio_worker, daemon=True).start()
    threading.Thread(target=_timeout_worker, daemon=True).start()
    if cap is not None and hasattr(cap, "read"):
        threading.Thread(target=_frame_reader,   daemon=True).start()
        threading.Thread(target=_qr_worker,      daemon=True).start()
        if person_detector is not None:
            threading.Thread(target=_presence_worker, daemon=True).start()
    _done.wait()
    return _pcm[0], _qr[0], _left[0]


def play_audio(wav_bytes: bytes, voice: "AudioPlayer") -> None:
    """WAVバイト列をスピーカーで再生する（WSL2対応）"""
    try:
        voice._play_wav(wav_bytes)
    except Exception as e:
        print(f"  [Access] 音声出力スキップ: {e}")


# ---------------------------------------------------------------------------
# モーター制御（GPIO）
# ---------------------------------------------------------------------------

def motor_idle() -> None:
    """待機ポーズ（首を正面に戻すなど）"""
    try:
        import RPi.GPIO as GPIO
        # TODO: 実機のピン番号・動作に合わせて実装
    except ImportError:
        pass  # 非ラズパイ環境はスキップ


def motor_greeting() -> None:
    """挨拶モーション（頭を傾けるなど）"""
    try:
        import RPi.GPIO as GPIO
        # TODO: 実機に合わせて実装
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# WebSocket セッション（1来訪者分）
# ---------------------------------------------------------------------------

async def run_ws_session(ws, voice: "AudioPlayer",
                        qr_initial: str | None = None,
                        mock_audio: bool = False,
                        audio_device: int | None = None,
                        detector=None,
                        rv_task: asyncio.Task | None = None) -> None:
    """
    GyomuSystemとのWebSocketセッションを1来訪者分処理する。
    mock_audio=True のとき: 音声の代わりにキーボード入力/テキスト表示でテスト可能。
    カメラがある場合はセッション全体を通じて来訪者の存在を常時監視する。
    """
    motor_greeting()

    start_msg: dict = {"type": "session_start"}
    if qr_initial:
        start_msg["qr"] = qr_initial
    await ws.send(json.dumps(start_msg))

    # 顔認識が完了したら visitor_hint をGyomuSystemへ送信（挨拶再生中に完了する）
    if rv_task is not None:
        async def _send_visitor_hint():
            try:
                rv = await asyncio.wait_for(rv_task, timeout=5.0)
                if rv:
                    # & = % は区切りと衝突するため最小エスケープ（読み取り側 parse_qs が自動デコード）
                    def _qr_escape(v):
                        return str(v).replace("%", "%25").replace("&", "%26").replace("=", "%3D")
                    parts = [f"name={_qr_escape(rv['name'])}"]
                    if rv.get("company"):
                        parts.append(f"company={_qr_escape(rv['company'])}")
                    await ws.send(json.dumps({
                        "type": "visitor_hint",
                        "visitor": "VISITOR:" + "&".join(parts),
                    }))
                    print(f"  🧠 [来訪者顔認識] {rv['name']} → visitor_hint 送信", flush=True)
            except (asyncio.TimeoutError, Exception):
                pass
        asyncio.create_task(_send_visitor_hint())

    _audio_device = audio_device
    _cap  = getattr(detector, "_cap",  None)
    _cv2  = getattr(detector, "_cv2",  None)

    # ----------------------------------------------------------------
    # ウォームマイク — ぴっぴ発話中も常時録音してプリバッファを蓄積する
    # _warm_buf の各要素は (chunk, pippi_was_speaking) のタプル。
    # ぴっぴ発話中のチャンクはエコーベースライン推定に使い、seed には含めない。
    # ----------------------------------------------------------------
    _warm_buf:      list           = []   # (chunk, pippi_was_speaking: bool) tuples
    _warm_lock:     threading.Lock = threading.Lock()
    _warm_stop:     threading.Event = threading.Event()
    _warm_started   = False
    _warm_rate:     list = [16000]        # バージイン用サンプルレート（ワーカーが設定）
    _pippi_speaking: list = [False]       # ぴっぴ音声再生中フラグ（スレッド間共有）

    def _warm_mic_worker():
        try:
            dev_info = sd.query_devices(audio_device, "input") if audio_device is not None \
                       else sd.query_devices(kind="input")
            rate = int(dev_info["default_samplerate"])
            _warm_rate[0] = rate
            # バージイン対応: ぴっぴ発話中（最大5秒）の来訪者発話も保持できるよう拡大
            limit = int(5.0 * rate / _VAD_CHUNK)
            with sd.InputStream(samplerate=rate, channels=CHANNELS, dtype=DTYPE,
                                blocksize=_VAD_CHUNK, device=audio_device) as stream:
                while not _warm_stop.is_set():
                    chunk, _ = stream.read(_VAD_CHUNK)
                    with _warm_lock:
                        _warm_buf.append((chunk.copy(), _pippi_speaking[0]))
                        if len(_warm_buf) > limit:
                            _warm_buf.pop(0)
        except Exception:
            pass

    def _start_warm_mic():
        nonlocal _warm_started
        if _warm_started or mock_audio:
            return
        _warm_started = True
        threading.Thread(target=_warm_mic_worker, daemon=True, name="WarmMic").start()

    def _stop_warm_mic_and_get_seed() -> list:
        nonlocal _warm_started
        _warm_stop.set()
        _warm_started = False   # 次の _start_warm_mic() でワーカーを再起動できるよう
        with _warm_lock:
            # ぴっぴ発話中のチャンクは seed に含めない（エコーがノイズフロア推定に混入するのを防ぐ）
            seed = [
                c for c, was_pippi in _warm_buf
                if not was_pippi and float(np.sqrt(np.mean(c.astype(np.float32) ** 2))) > 10
            ]
            _warm_buf.clear()   # 古い音声を次のバージイン検知に引き継がない
        _warm_stop.clear()      # 次のワーカーが即時終了しないようリセット
        return seed

    def _extract_barge_in_speech() -> tuple[bytes | None, int]:
        """warm_buf からぴっぴ発話中の来訪者割り込み音声を抽出する。
        ぴっぴ発話中チャンクの中央値 RMS をエコーベースラインとして閾値を算出するため、
        スピーカーとマイクの距離にかかわらず来訪者の声だけを検知できる。
        連続3チャンク以上で閾値を超えるセグメントを確定（約95ms）。
        Returns: (pcm_16kHz_int16_bytes, sample_rate) — 未検知なら (None, rate)
        """
        with _warm_lock:
            buf = list(_warm_buf)  # (chunk, pippi_was_speaking) tuples
        rate = _warm_rate[0]
        if len(buf) < 6:
            return None, rate

        rms_list = [float(np.sqrt(np.mean(c.astype(np.float32) ** 2))) for c, _ in buf]

        # ぴっぴ発話中チャンクの RMS からエコーベースラインを推定。
        # ぴっぴデータが少ない場合は全チャンクの中央値にフォールバック。
        pippi_rms = [rms for rms, (_, is_pippi) in zip(rms_list, buf) if is_pippi]
        if len(pippi_rms) >= 4:
            sorted_pippi = sorted(pippi_rms)
            baseline = sorted_pippi[len(sorted_pippi) // 2]
        else:
            sorted_all = sorted(rms_list)
            baseline = sorted_all[len(sorted_all) // 2]
        threshold = max(baseline * 2.5, _VAD_THRESHOLD)

        # 連続3チャンク以上で閾値を超えるセグメントを探す。
        # ぴっぴ発話中チャンクも含めて全チャンクを対象にする（バージインは発話中に起きる）。
        CONFIRM     = 3
        speech_start: int | None = None
        speech_end:   int | None = None
        consecutive = 0
        for idx, rms in enumerate(rms_list):
            if rms >= threshold:
                consecutive += 1
                if consecutive >= CONFIRM and speech_start is None:
                    speech_start = max(0, idx - CONFIRM + 1)
                if speech_start is not None:
                    speech_end = idx + 1
            else:
                if consecutive < CONFIRM:
                    consecutive = 0

        if speech_start is None or speech_end is None:
            return None, rate

        speech_chunks = [c for c, _ in buf[speech_start:speech_end]]
        audio = np.concatenate([c.flatten() for c in speech_chunks]).astype(np.float32)
        if rate != SAMPLE_RATE:
            n_out = int(len(audio) * SAMPLE_RATE / rate)
            audio = np.interp(
                np.linspace(0, len(audio) - 1, n_out),
                np.arange(len(audio)),
                audio,
            )
        pcm = audio.astype(np.int16).tobytes()
        print(f"  [Access] バージイン検知: {len(speech_chunks)}チャンク, 閾値={threshold:.0f}", flush=True)
        return pcm, SAMPLE_RATE

    # ----------------------------------------------------------------
    # セッション全体の常時カメラ監視
    # ----------------------------------------------------------------
    _visitor_gone = asyncio.Event()   # 来訪者が立ち去ったらセット
    _monitor_stop = asyncio.Event()   # モニター停止用
    _recording    = asyncio.Event()   # 録音中はTrueにしてモニターを一時停止

    async def _presence_monitor():
        """録音中以外の全タイミングでカメラを監視し、退出を検知する。"""
        if not (detector and hasattr(detector, "_detect_presence")):
            return

        # フレーム取得: capture_thread がある場合はバッファ経由、なければ直接読み取り
        def _get_frame_safe():
            if hasattr(detector, "_get_frame"):
                return detector._get_frame()
            if _cap is not None:
                ret, f = _cap.read()
                return f if ret else None
            return None

        absent        = 0
        present_streak = 0
        ABSENT_LIMIT   = 10  # 0.3s × 10 ≈ 3秒連続不在で退出とみなす
        PRESENT_CONFIRM = 2
        while not _monitor_stop.is_set():
            if _recording.is_set():
                present_streak = 0
                await asyncio.sleep(0.5)
                continue
            try:
                frame = await asyncio.to_thread(_get_frame_safe)
                if frame is not None:
                    present = await asyncio.to_thread(detector._detect_presence, frame)
                    if present:
                        present_streak += 1
                        if present_streak >= PRESENT_CONFIRM:
                            absent = 0
                    else:
                        present_streak = 0
                        absent += 1
                        if absent >= ABSENT_LIMIT:
                            print("  [Access] 来訪者退出検知（常時監視）", flush=True)
                            _visitor_gone.set()
                            return
            except Exception:
                pass
            await asyncio.sleep(0.3)

    _presence_task = asyncio.create_task(_presence_monitor())

    async def _recv_or_gone():
        """ws.recv() と退出イベントを競合させる。退出検知時は None を返す。"""
        _rt = asyncio.ensure_future(ws.recv())
        _gt = asyncio.ensure_future(_visitor_gone.wait())
        await asyncio.wait([_rt, _gt], return_when=asyncio.FIRST_COMPLETED)
        _gt.cancel()
        try:
            await _gt
        except (asyncio.CancelledError, Exception):
            pass
        if _visitor_gone.is_set():
            _rt.cancel()
            try:
                await _rt
            except (asyncio.CancelledError, Exception):
                pass
            return None
        return await _rt  # 例外があればここで伝播

    # ----------------------------------------------------------------
    # メインループ
    # ----------------------------------------------------------------
    try:
        while True:
            raw = await _recv_or_gone()

            # 退出検知（録音中以外）
            if raw is None:
                print("  [Access] 退出通知 → GyomuSystem", flush=True)
                try:
                    await ws.send(json.dumps({"type": "visitor_left"}))
                except Exception:
                    pass
                motor_idle()
                return

            if isinstance(raw, str):
                msg = json.loads(raw)
                mtype = msg.get("type")

                if mtype == "session_end":
                    print("  [Access] セッション終了")
                    motor_idle()
                    return

                elif mtype == "speaking":
                    print(f"\nぴっぴ: {msg['text']}", flush=True)

                elif mtype == "request_audio":
                    _delay_ms = msg.get("delay_ms", 0)
                    # 退出待ち（closing）中は、退出検知が失敗しても次の来訪者を
                    # 長く待たせないよう打ち切りを短くする（通常ターンは余裕を持たせる）
                    _cap_timeout = 15.0 if msg.get("closing") else 120.0
                    if _delay_ms > 0:
                        await asyncio.sleep(_delay_ms / 1000)
                    if mock_audio:
                        _input_task = asyncio.create_task(
                            asyncio.to_thread(input, "あなた: ")
                        )
                        _cancelled = False
                        _timed_out = False
                        _waited = 0.0
                        while not _input_task.done():
                            try:
                                _peek = await asyncio.wait_for(ws.recv(), timeout=0.2)
                                if isinstance(_peek, str):
                                    _pmsg = json.loads(_peek)
                                    if _pmsg.get("type") == "cancel_audio":
                                        _input_task.cancel()
                                        _cancelled = True
                                        staff_text = _pmsg.get("staff_msg", "")
                                        print(f"\n  [スタッフ割り込み] {staff_text}")
                                        break
                                    elif _pmsg.get("type") == "speaking":
                                        print(f"\nぴっぴ: {_pmsg['text']}", flush=True)
                            except asyncio.TimeoutError:
                                pass
                            # 退出待ち（closing）中に入力が無いまま打ち切り時間を過ぎたら
                            # 無音通知を送ってセッションを終わらせる（mockでも退出待ちから抜ける）
                            _waited += 0.2
                            if msg.get("closing") and _waited >= _cap_timeout:
                                _input_task.cancel()
                                _timed_out = True
                                print("  [Access] (mock) 退出待ちタイムアウト → 無音通知", flush=True)
                                break
                        if _timed_out:
                            await ws.send(json.dumps({"type": "silence_timeout"}))
                        elif not _cancelled:
                            try:
                                text = (await _input_task).strip()
                            except (asyncio.CancelledError, EOFError):
                                text = ""
                            await ws.send(json.dumps({"type": "text_input", "text": text}))
                    elif _cap is not None and _cv2 is not None:
                        # ウォームマイクを止めてプリバッファを取得
                        seed = _stop_warm_mic_and_get_seed()
                        # 録音中フラグをセット（常時監視モニターを一時停止）
                        _recording.set()
                        print("  [Access] 録音中（QR/退出も監視）...", flush=True)
                        try:
                            pcm, qr, visitor_left = await asyncio.to_thread(
                                capture_audio_or_qr, _audio_device, _cap, _cv2, detector, seed, _cap_timeout
                            )
                        finally:
                            _recording.clear()
                        if visitor_left:
                            print("  [Access] 来訪者退出（録音中検知） → GyomuSystem通知", flush=True)
                            await ws.send(json.dumps({"type": "visitor_left"}))
                            motor_idle()
                            return
                        elif qr:
                            print(f"  [Access] QR検知: {qr[:60]}", flush=True)
                            await ws.send(json.dumps({"type": "text_input", "text": qr}))
                        elif pcm:
                            await ws.send(pcm)
                        else:
                            # 発話・QR・退出のいずれも無くタイムアウト打ち切り
                            # → 無音通知（closing中はサーバーの安全弁がセッションを終了する）
                            print("  [Access] 無入力タイムアウト → 無音通知", flush=True)
                            await ws.send(json.dumps({"type": "silence_timeout"}))
                    else:
                        print("  [Access] 録音中...", flush=True)
                        pcm = capture_audio(device=_audio_device, max_polls=1)
                        if pcm is None:
                            await ws.send(json.dumps({"type": "silence_timeout"}))
                        else:
                            await ws.send(pcm)

                elif mtype == "cancel_audio":
                    print(f"\n  [スタッフ割り込み] {msg.get('staff_msg', '')}")

                elif mtype == "check_barge_in":
                    # ぴっぴ発話中の来訪者割り込みを検出してGyomuSystemへ返す
                    pcm, rate = _extract_barge_in_speech()
                    if pcm:
                        with _warm_lock:
                            _warm_buf.clear()  # 同じ音声を次の文で再検知しない
                        await ws.send(json.dumps({
                            "type":     "barge_in_result",
                            "detected": True,
                            "pcm":      base64.b64encode(pcm).decode(),
                            "rate":     rate,
                        }))
                    else:
                        await ws.send(json.dumps({
                            "type":     "barge_in_result",
                            "detected": False,
                        }))

                elif mtype == "error":
                    print(f"  [Access] クラウドエラー: {msg.get('detail')}")
                    motor_idle()
                    return

            # バイナリ = GyomuSystemからのWAV音声データ
            else:
                _start_warm_mic()  # ぴっぴ発話開始と同時にマイクをウォームアップ
                # 発話再生中は常時監視を一時停止（来訪者が聞いている間に顔検出が
                # 一瞬外れても誤って退出と判定しないようにする。挨拶直後の誤退出対策）
                _pippi_speaking[0] = True
                _recording.set()
                try:
                    await asyncio.to_thread(play_audio, raw, voice)
                finally:
                    _pippi_speaking[0] = False
                    _recording.clear()

    finally:
        _warm_stop.set()
        _monitor_stop.set()
        _presence_task.cancel()
        try:
            await _presence_task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# カメラストリーム送信（GyomuSystem へ JPEG を WebSocket で投げ続ける）
# ---------------------------------------------------------------------------

async def _stream_camera(detector, cam_http_url: str, api_key: str = "") -> None:
    """detector の最新フレームをJPEGに圧縮して HTTP POST でGyomuSystemへ送信し続ける。
    送信ループと撮影ループを分離し、送信待ちでフレームが詰まらないようにする。"""
    import cv2 as _cv2

    _latest: list = [None]  # 1スロットバッファ：常に最新フレームだけ保持
    # /api/camera/push はサーバー側で認証必須。X-API-Key を毎フレーム付与する。
    _post_headers = {"Content-Type": "image/jpeg"}
    if api_key:
        _post_headers["X-API-Key"] = api_key

    async def _sender():
        """バッファから最新JPEGを取り出してPOSTし続ける（aiohttp でネイティブ非同期送信）"""
        import aiohttp
        _err = False
        async with aiohttp.ClientSession() as session:
            while True:
                b = _latest[0]
                if b is None:
                    await asyncio.sleep(0.01)
                    continue
                _latest[0] = None
                try:
                    async with session.post(
                        cam_http_url, data=b,
                        headers=_post_headers,
                        timeout=aiohttp.ClientTimeout(total=3),
                    ):
                        pass
                    if _err:
                        print("  [Camera] 送信再開", flush=True)
                        _err = False
                except Exception as e:
                    if not _err:
                        print(f"  [Camera] 送信失敗 ({e})", flush=True)
                        _err = True

    asyncio.create_task(_sender())
    print(f"  [Camera] ストリーム開始 → {cam_http_url}", flush=True)

    while True:
        frame = detector.get_latest_frame()
        if frame is None:
            await asyncio.sleep(0.033)
            continue
        ok, jpg = await asyncio.to_thread(
            lambda f=frame: _cv2.imencode(
                ".jpg",
                _cv2.resize(f, (640, 480)),
                [_cv2.IMWRITE_JPEG_QUALITY, 40],
            )
        )
        if ok:
            _latest[0] = jpg.tobytes()  # 古いフレームを上書きして常に最新を保持
        await asyncio.sleep(0.033)  # 約30fps → 実効15fps（送信コスト分を考慮）


# ---------------------------------------------------------------------------
# メインループ（idle → 検知 → WS接続 → 会話 → idle）
# ---------------------------------------------------------------------------

async def _fetch_cloud_speaker_id(gyomu_url: str, default: int) -> tuple[int, bool]:
    """起動時にGyomuSystemからvoicevox_speaker_id設定を取得してキャッシュする。
    Returns: (speaker_id, success)  失敗した場合は (default, False)"""
    try:
        async with ws_connect(gyomu_url, open_timeout=5) as ws:
            await ws.send(json.dumps({"type": "get_config"}))
            raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
            msg = json.loads(raw)
            if msg.get("type") == "config":
                sid = int(msg.get("voicevox_speaker_id", default))
                print(f"  [Access] クラウド設定取得: voicevox_speaker_id={sid}", flush=True)
                return sid, True
    except Exception as e:
        print(f"  [Access] クラウド設定取得失敗（デフォルト {default} を使用）: {e}", flush=True)
    return default, False


def _staff_embeddings_url(cam_url: str | None, gyomu_url: str) -> str | None:
    """スタッフ顔エンベディング配信エンドポイントのURLを組み立てる。
    cam-url（HTTPサーバー）優先。無ければ gyomu-url のホストに :8000 を仮定する。"""
    from urllib.parse import urlparse
    base = None
    if cam_url:
        p = urlparse(cam_url)
        if p.scheme and p.netloc:
            base = f"{p.scheme}://{p.netloc}"
    if base is None and gyomu_url:
        host = urlparse(gyomu_url).hostname or "localhost"
        base = f"http://{host}:8000"  # WS(8001) と対になる FastAPI は 8000
    if base is None:
        return None
    return base.rstrip("/") + "/api/face/staff/embeddings"


def _sync_staff_embeddings(cam_url: str | None, gyomu_url: str, api_key: str = "") -> dict:
    """起動時にクラウドからスタッフ顔エンベディングを取得し、
    client/data/staff_embeddings.json に保存する（スタッフ照合のローカル同期）。
    失敗しても致命的でないため、前回分があればそれを使い続ける。
    Returns: {"ok": bool, "detail": str}"""
    import urllib.request
    from pippi_client.vision.face_auth import _EMBED_CACHE_FILE
    url = _staff_embeddings_url(cam_url, gyomu_url)
    if not url:
        print("  [スタッフ同期] 同期先URLを特定できず → スキップ", flush=True)
        return {"ok": False, "detail": "同期先URLを特定できず"}
    try:
        req = urllib.request.Request(url)
        if api_key:
            req.add_header("X-API-Key", api_key)
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        _EMBED_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _EMBED_CACHE_FILE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        print(f"  [スタッフ同期] {len(rows)}件取得 → {_EMBED_CACHE_FILE.name}（{url}）", flush=True)
        return {"ok": True, "detail": f"{len(rows)}件取得"}
    except Exception as e:
        print(f"  [スタッフ同期] 取得失敗（前回分があれば継続使用）: {e}", flush=True)
        return {"ok": False, "detail": str(e)}


def _read_hw_stats() -> dict:
    """CPU温度・使用率・メモリ使用率を取得する。取得できない項目はスキップ。"""
    stats: dict = {}
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            stats["cpu_temp_c"] = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        pass
    try:
        import psutil
        stats["cpu_percent"] = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        stats["mem_percent"] = round(mem.percent, 1)
    except Exception:
        pass
    return stats


async def _heartbeat_loop(gyomu_url: str, api_key: str, interval_sec: int = 30) -> None:
    """30秒ごとにサーバーへハートビートを送信し、オンライン状態を通知する。"""
    import aiohttp
    from urllib.parse import urlparse
    host = urlparse(gyomu_url).hostname or "localhost"
    url = f"http://{host}:8000/api/pippi/heartbeat"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await session.post(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=5))
            except Exception:
                pass
            await asyncio.sleep(interval_sec)


async def _health_report_loop(gyomu_url: str, api_key: str,
                               interval_sec: int = 600) -> None:
    """定期的にCPU温度・使用率を管理システムへ送信するバックグラウンドループ。"""
    import aiohttp
    from urllib.parse import urlparse
    host = urlparse(gyomu_url).hostname or "localhost"
    url = f"http://{host}:8000/api/logs/system-events"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(interval_sec)
            stats = await asyncio.to_thread(_read_hw_stats)
            if not stats:
                continue
            parts = []
            if "cpu_temp_c" in stats:
                parts.append(f"CPU温度 {stats['cpu_temp_c']}℃")
            if "cpu_percent" in stats:
                parts.append(f"CPU {stats['cpu_percent']}%")
            if "mem_percent" in stats:
                parts.append(f"メモリ {stats['mem_percent']}%")
            print(f"  [ヘルス] {' / '.join(parts)}", flush=True)
            try:
                await session.post(
                    url,
                    json={"all_green": True, "details": {"type": "health", **stats}},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                )
            except Exception:
                pass


async def _post_boot_status(gyomu_url: str, api_key: str, checks: dict) -> None:
    """起動チェック結果を管理システムへ HTTP POST する。失敗してもラズパイ動作に影響しない。"""
    import aiohttp
    from urllib.parse import urlparse
    host = urlparse(gyomu_url).hostname or "localhost"
    url = f"http://{host}:8000/api/logs/system-events"
    all_green = all(v.get("ok") for v in checks.values())
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"all_green": all_green, "details": checks},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status in (200, 201):
                    status_str = "ALL GREEN ✅" if all_green else "一部NG ⚠️"
                    print(f"  [起動チェック] 管理システムへ送信完了 ({status_str})", flush=True)
                else:
                    print(f"  [起動チェック] 送信失敗 HTTP {resp.status}", flush=True)
    except Exception as e:
        print(f"  [起動チェック] 送信失敗: {e}", flush=True)


async def main_async(args: argparse.Namespace) -> None:
    detector_kwargs = {}
    if args.detector == "ultrasonic":
        detector_kwargs["threshold_cm"] = args.threshold
    elif args.detector in ("camera", "mediapipe", "qr"):
        # 数値文字列なら int に変換、URLならそのまま文字列で渡す
        try:
            detector_kwargs["camera_index"] = int(args.camera)
        except ValueError:
            detector_kwargs["camera_index"] = args.camera
        if args.detector == "mediapipe" and args.no_pose:
            detector_kwargs["use_pose"] = False

    detector = build_detector(args.detector, **detector_kwargs)
    _speaker_id, _cloud_ok = await _fetch_cloud_speaker_id(args.gyomu_url, args.speaker_id)
    voice = AudioPlayer(speaker_id=_speaker_id)

    # スタッフ顔エンベディングをクラウドから同期（client/data/staff_embeddings.json）
    _api_key = args.api_key or os.getenv("PIPPI_API_KEY", "")
    _sync_result = await asyncio.to_thread(_sync_staff_embeddings, args.cam_url, args.gyomu_url, _api_key)

    # カメラストリームをGyomuSystemへ送信するバックグラウンドタスク
    if hasattr(detector, "_start_capture_thread") and args.cam_url:
        # capture thread を先に起動して最初のフレームが取れてから wait_for_person を開始
        await asyncio.to_thread(detector._start_capture_thread)
        asyncio.create_task(_stream_camera(detector, args.cam_url, _api_key))

    # ----------------------------------------------------------------
    # 起動チェック（マイク・VOICEVOX・顔認証）
    # ----------------------------------------------------------------
    _boot_checks: dict = {
        "cloud_ws":   {"ok": _cloud_ok,
                       "detail": f"voicevox_speaker_id={_speaker_id}" if _cloud_ok else "接続失敗"},
        "staff_sync": _sync_result,
    }

    if args.mock_audio:
        _boot_checks["microphone"] = {"ok": True, "detail": "モック（キーボード入力）"}
    else:
        try:
            dev_info = sd.query_devices(args.audio_device, "input") if args.audio_device is not None \
                       else sd.query_devices(kind="input")
            _boot_checks["microphone"] = {"ok": True, "detail": dev_info["name"]}
        except Exception as e:
            _boot_checks["microphone"] = {"ok": False, "detail": str(e)}

    # VOICEVOX はサーバー側で動作するためクライアントではチェックしない

    # 顔認証関数を事前ロード（初回importのウォームアップ）
    _has_face_auth = hasattr(detector, "get_latest_frame")
    _recognize_staff   = None
    _recognize_visitor = None
    if _has_face_auth:
        try:
            from pippi_client.vision.face_auth import recognize_staff as _recognize_staff
            _boot_checks["face_auth"] = {"ok": True, "detail": "スタッフ顔照合 準備完了"}
        except ImportError as e:
            _boot_checks["face_auth"] = {"ok": False, "detail": f"インポート失敗 ({e})"}

    # バナー + 起動チェック結果を表示
    _check_labels = {
        "cloud_ws":   "クラウドWS ",
        "staff_sync": "スタッフ同期",
        "microphone": "マイク     ",
        "face_auth":  "顔認証     ",
    }
    print("=" * 50)
    print("  ぴっぴ Access — ラズパイモード")
    print(f"  検知方式  : {args.detector}")
    print(f"  GyomuURL  : {args.gyomu_url}")
    print("-" * 50)
    print("  [起動チェック]")
    for key, val in _boot_checks.items():
        icon  = "✅" if val["ok"] else "❌"
        label = _check_labels.get(key, key)
        print(f"    {icon} {label}: {val['detail']}")
    print("=" * 50)
    print("終了するには Ctrl+C\n")

    # 起動チェック結果を管理システムへ送信 + 定期ヘルスレポート開始
    await _post_boot_status(args.gyomu_url, _api_key, _boot_checks)
    asyncio.create_task(_health_report_loop(args.gyomu_url, _api_key))
    asyncio.create_task(_heartbeat_loop(args.gyomu_url, _api_key))

    print("ぴっぴ: idle 待機中だっぴ〜\n")

    motor_idle()

    import time as _time
    _staff_greet_date: dict = {}   # staff_id -> last greeted date (YYYY-MM-DD)
    _staff_auth_time:  dict = {}   # staff_id -> last recognized timestamp（認証スキップ用）
    _FACE_AUTH_SKIP = 20.0         # 認識済みスタッフは20秒間再スキャンしない
    _face_fail_time: float = 0.0   # 直近の認証失敗タイムスタンプ
    _FACE_FAIL_COOLDOWN = 3.0      # 認証失敗後のクールダウン（秒）

    try:
        while True:
            # 2秒タイムアウトで人検知待機（Ctrl+C に確実に応答できる）
            detected = await asyncio.to_thread(detector.wait_for_person, 2.0)

            # 人を検知したときだけ顔認証スキャン（タイムアウト時はスキップ）
            if not detected:
                continue

            if _recognize_staff:
                frame = detector.get_latest_frame()
                if frame is not None:
                    now = _time.time()
                    # 直近20秒以内に認識済みのスタッフがいればスキャンをスキップ
                    _recent_staff_id = next(
                        (sid for sid, t in _staff_auth_time.items() if now - t < _FACE_AUTH_SKIP), None
                    )
                    if _recent_staff_id is not None:
                        continue
                    # 直近の認証失敗クールダウン中はスキャンをスキップ
                    if now - _face_fail_time < _FACE_FAIL_COOLDOWN:
                        continue

                    # スタッフ照合（類似度50〜60%はリトライ）
                    try:
                        staff = await asyncio.to_thread(lambda f=frame: _recognize_staff(frame=f))
                        if isinstance(staff, dict) and staff.get("retry"):
                            print("  [顔認証] リトライ中（0.5秒後に再取得）...", flush=True)
                            await asyncio.sleep(0.5)
                            _retry_frame = detector.get_latest_frame()
                            if _retry_frame is not None:
                                staff = await asyncio.to_thread(
                                    lambda f=_retry_frame: _recognize_staff(frame=f)
                                )
                            else:
                                staff = None
                        # リトライ後も閾値未達なら不一致として扱う（クールダウン開始）
                        if isinstance(staff, dict) and staff.get("retry"):
                            staff = None
                            _face_fail_time = _time.time()
                    except Exception as _e:
                        print(f"  [顔認証] スタッフ照合エラー: {_e}", flush=True)
                        staff = None
                    if staff is None:
                        _face_fail_time = _time.time()
                    if staff:
                        sid = staff["id"]
                        today = _time.strftime("%Y-%m-%d")

                        # 挨拶（1日1回のみ）
                        if _staff_greet_date.get(sid) != today:
                            _staff_greet_date[sid] = today
                            _hour = int(_time.strftime("%H"))
                            _salutation = (
                                "おはようございます" if _hour < 11 else
                                "こんにちは"         if _hour < 18 else
                                "こんばんは"
                            )
                            msg = f"{_salutation}、{staff['name']}さんっぴ〜！"
                            print(f"  👤 [スタッフ認識] {staff['name']}: {msg}", flush=True)
                            if await asyncio.to_thread(voice.is_available):
                                await asyncio.to_thread(voice.speak, msg)
                            else:
                                try:
                                    async with ws_connect(args.gyomu_url, open_timeout=5) as _ws:
                                        await _ws.send(json.dumps({"type": "staff_greet", "text": msg}))
                                        _wav = await asyncio.wait_for(_ws.recv(), timeout=10.0)
                                        if isinstance(_wav, bytes):
                                            await asyncio.to_thread(play_audio, _wav, voice)
                                except Exception as _ge:
                                    print(f"  [スタッフ挨拶] 音声送信失敗: {_ge}", flush=True)
                        else:
                            print(f"  👤 [スタッフ認識] {staff['name']}: 2回目以降", flush=True)

                        # WSセッション（毎回）
                        async def _open_staff_ws():
                            return await ws_connect(args.gyomu_url, open_timeout=12)
                        _ws_conn_task = asyncio.create_task(_open_staff_ws())

                        # 音声待機（挨拶・WS接続と並列）
                        _ss_stop = threading.Event()
                        threading.Timer(5.0, _ss_stop.set).start()
                        print("  [スタッフセッション] 音声待機中（5秒）...", flush=True)
                        _staff_pcm = await asyncio.to_thread(
                            _capture_once, args.audio_device, _ss_stop
                        )
                        _ss_stop.set()

                        # RMSが低い雑音はSTTに送らず処分
                        if _staff_pcm:
                            import numpy as _np
                            _rms = float(_np.sqrt(_np.mean(
                                _np.frombuffer(_staff_pcm, dtype=_np.int16).astype(_np.float32) ** 2
                            )))
                            if _rms < 300:
                                print(f"  [スタッフセッション] 雑音除去（RMS={_rms:.0f}）", flush=True)
                                _staff_pcm = None

                        # 発話が取れていればスタッフはいると見なす（発話中の動きで誤不在判定を防ぐ）
                        # 無音だった場合のみカメラで確認
                        _ss_present = True
                        if not _staff_pcm and _has_face_auth:
                            _ss_frame = detector.get_latest_frame()
                            if _ss_frame is not None:
                                try:
                                    _ss_present = await asyncio.to_thread(
                                        detector._detect_presence, _ss_frame
                                    )
                                except Exception:
                                    _ss_present = True

                        if not _ss_present:
                            _ws_conn_task.cancel()
                        else:
                            _send_pcm = _staff_pcm or bytes(int(0.2 * SAMPLE_RATE) * 2)
                            try:
                                # 事前確立済みWSを取得（タイムアウトは短くてよい）
                                _ss_ws = await asyncio.wait_for(_ws_conn_task, timeout=3.0)
                                try:
                                    await _ss_ws.send(json.dumps({
                                        "type": "staff_session",
                                        "staff_id": sid,
                                        "staff_name": staff["name"],
                                    }))
                                    await _ss_ws.send(_send_pcm)
                                    while True:
                                        try:
                                            _ss_raw = await asyncio.wait_for(
                                                _ss_ws.recv(), timeout=15.0
                                            )
                                        except asyncio.TimeoutError:
                                            break
                                        if isinstance(_ss_raw, bytes):
                                            await asyncio.to_thread(play_audio, _ss_raw, voice)
                                        elif isinstance(_ss_raw, str):
                                            _ss_msg = json.loads(_ss_raw)
                                            if _ss_msg.get("type") == "speaking":
                                                print(f"\nぴっぴ: {_ss_msg['text']}", flush=True)
                                            elif _ss_msg.get("type") == "session_end":
                                                break
                                            elif _ss_msg.get("type") == "request_audio":
                                                _req_stop = threading.Event()
                                                threading.Timer(8.0, _req_stop.set).start()
                                                _req_pcm = await asyncio.to_thread(
                                                    _capture_once, args.audio_device, _req_stop
                                                )
                                                _req_stop.set()
                                                await _ss_ws.send(
                                                    _req_pcm or bytes(int(0.2 * SAMPLE_RATE) * 2)
                                                )
                                except WsConnectionClosed:
                                    pass
                                finally:
                                    await _ss_ws.close()
                            except Exception as _se:
                                print(f"  [スタッフセッション] 接続失敗: {_se}", flush=True)

                        _staff_auth_time[sid] = _time.time()  # 挨拶・セッション完了後に更新
                        continue  # スタッフは来訪者セッションを開始しない

                    # 来訪者照合はクラウド（GyomuSystem）がカメラフレームから実行するため、
                    # クライアントでは行わない（_rv_task は常に None）。
                    _rv_task = None
                else:
                    _rv_task = None
            else:
                _rv_task = None

            # QRコードのみ優先。来訪者顔認識は visitor_hint メッセージで非同期注入
            if hasattr(detector, "last_qr_data") and detector.last_qr_data:
                qr_data = detector.last_qr_data
                if _rv_task is not None:
                    _rv_task.cancel()
                    _rv_task = None
            else:
                qr_data = None

            print(f"\n  [Access] 人を検知 → クラウド接続中...", flush=True)
            try:
                async with ws_connect(args.gyomu_url, open_timeout=30) as ws:
                    await run_ws_session(ws, voice, qr_initial=qr_data,
                                         mock_audio=args.mock_audio,
                                         audio_device=args.audio_device,
                                         detector=detector,
                                         rv_task=_rv_task)
            except WsConnectionClosed:
                print("  [Access] サーバー切断")
            except Exception as e:
                print(f"  [Access] 接続エラー: {e}")
                motor_idle()

            # カメラ検知の場合、同一人物での再起動を防ぐ
            if hasattr(detector, "wait_until_clear"):
                detector.wait_until_clear(timeout=30.0)

            print("ぴっぴ: idle 待機中だっぴ〜\n")

    except KeyboardInterrupt:
        print("\nシャットダウンするっぴ〜")
    finally:
        detector.cleanup()


def main() -> None:
    # Windows の ProactorEventLoop は WebSocket と相性が悪いため SelectorEventLoop を使用
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    parser = argparse.ArgumentParser(description="ぴっぴ Access（ラズパイ側）")
    parser.add_argument("--detector",   default="mock",
                        choices=["mock", "ultrasonic", "camera", "mediapipe", "pir", "qr"])
    parser.add_argument("--threshold",  type=float, default=150.0)
    parser.add_argument("--camera",     default="0",
                        help="カメラ番号（0,1...）またはIPカメラURL（http://192.168.x.x:port/video）")
    parser.add_argument("--gyomu-url",   default="ws://localhost:8001",
                        help="GyomuSystem WebSocket URL")
    parser.add_argument("--speaker-id", type=int, default=69,
                        help="VOICEVOXスピーカーID（デフォルト: 69 満別花丸ノーマル）")
    parser.add_argument("--mock-audio", action="store_true",
                        help="マイク入力をキーボードで代替（音声出力は通常通り再生）")
    parser.add_argument("--audio-device", type=int, default=None,
                        help="マイクのデバイス番号（省略時はシステムデフォルト）。--list-devices で確認")
    parser.add_argument("--list-devices", action="store_true",
                        help="利用可能な音声デバイス一覧を表示して終了")
    parser.add_argument("--cam-url", default=None,
                        help="カメラフレーム送信先 HTTP URL（例: http://localhost:8000/api/camera/push）")
    parser.add_argument("--no-pose", action="store_true",
                        help="Pose推定を無効化（WSL2環境でMediaPipeが固まる場合に使用）")
    parser.add_argument("--api-key", default="",
                        help="サーバー API キー（未指定時は環境変数 PIPPI_API_KEY を使用）")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
