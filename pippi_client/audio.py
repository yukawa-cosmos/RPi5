"""
クライアント（ラズパイ）用 軽量オーディオプレイヤ

クラウド（GyomuSystem）が合成して送ってくる WAV/PCM を再生するだけ。
音声合成（VOICEVOX/HTTP）や DB 参照は持たない（それらはサーバー側）。

設計上の互換ポイント:
  - Access は従来 VoiceVox インスタンスを介して再生していた（voice._play_wav / play_audio）。
    本クラスは同じ用途を肩代わりする差し替え先。
  - is_available() は常に False を返す（クライアントにローカル合成は無い）。
    → Access のフォールowは「クラウドに合成を依頼して再生」へ自然に流れる。
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import wave


class AudioPlayer:
    """受信した WAV バイト列を再生するだけの軽量プレイヤ。"""

    def __init__(self, speaker_id: int | None = None):
        # speaker_id はクラウド合成のためのヒントとして保持するだけ（再生には使わない）
        self.speaker_id = speaker_id

    # --- Access 互換 API ---------------------------------------------------
    def is_available(self) -> bool:
        """クライアントにローカル音声合成は無い。常に False。"""
        return False

    def speak(self, text: str) -> None:  # pragma: no cover - 呼ばれない想定
        """ローカル合成は非対応（クラウドが合成して WAV を送る設計）。"""
        raise NotImplementedError("クライアントはローカル合成しない（クラウドが合成する）")

    def _play_wav(self, wav_data: bytes) -> None:
        """Access 既存コードとの互換用エイリアス。"""
        self.play_wav(wav_data)

    # --- 実装 --------------------------------------------------------------
    def play_wav(self, wav_data: bytes) -> None:
        """WAV バイト列を再生する（sounddevice を優先、ダメなら aplay にフォールバック）。"""
        try:
            self._play_sounddevice(wav_data)
            return
        except Exception as e:
            print(f"  [音声] sounddevice 再生失敗 → aplay を試行: {e}", flush=True)
        self._play_aplay(wav_data)

    @staticmethod
    def _play_sounddevice(wav_data: bytes) -> None:
        import numpy as np
        import sounddevice as sd
        with wave.open(io.BytesIO(wav_data), "rb") as wf:
            rate = wf.getframerate()
            channels = wf.getnchannels()
            frames = wf.readframes(wf.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            audio = audio.reshape(-1, channels)
        sd.play(audio, rate)
        sd.wait()

    @staticmethod
    def _play_aplay(wav_data: bytes) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_data)
            tmp_path = f.name
        try:
            subprocess.run(["aplay", "-q", tmp_path], check=True, timeout=60)
        finally:
            os.unlink(tmp_path)
