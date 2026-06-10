"""
スタッフ顔照合モジュール（クライアント / ラズパイ ローカル実行版）

クラウドが生成・同期した staff_embeddings.json を読み、カメラ映像と突き合わせて
スタッフを特定する。SQLite には一切触らない（DB不要）。

設計（client=検知＋スタッフ照合 / cloud=来訪者照合・学習）:
  - スタッフの顔登録・埋め込み生成・JSON書き出しはすべてクラウド側（server/app/vision/face_auth.py）。
  - クライアントは client/data/staff_embeddings.json を読むだけ（デプロイ時／スタッフ追加時に同期）。
  - 来訪者照合は行わない（クラウド GyomuSystem が担当）。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Optional

# client/data/staff_embeddings.json （クラウドから同期される）
_EMBED_CACHE_FILE = Path(__file__).parent.parent.parent / "data" / "staff_embeddings.json"
_MODEL = "Facenet"
_THRESHOLD = 0.40
_RETRY_THRESHOLD = 0.50  # 類似度50〜60%はリトライ候補

# メモリキャッシュ
_embedding_cache: list = []
_cache_loaded_at: float = 0.0
_CACHE_TTL = 30.0


def _get_candidates() -> list:
    """JSONキャッシュファイル（クラウド同期）から埋め込みを読む。DBには触らない。"""
    global _embedding_cache, _cache_loaded_at
    now = time.time()
    if now - _cache_loaded_at < _CACHE_TTL and _embedding_cache:
        return _embedding_cache
    try:
        if _EMBED_CACHE_FILE.exists():
            _embedding_cache = json.loads(_EMBED_CACHE_FILE.read_text(encoding="utf-8"))
            _cache_loaded_at = now
            print(f"  [スタッフ照合] キャッシュ読み込み: {len(_embedding_cache)}件", flush=True)
        else:
            # クライアントでは JSON が無ければ照合不可（クラウドから同期されるまで待つ）
            print(f"  [スタッフ照合] {_EMBED_CACHE_FILE.name} 未同期 → 照合スキップ", flush=True)
            _embedding_cache = []
            _cache_loaded_at = now
    except Exception as e:
        print(f"  [スタッフ照合] キャッシュ読み込み失敗（前回分を継続使用）: {e}", flush=True)
    return _embedding_cache


def _extract_embedding(img_path: str) -> Optional[list]:
    try:
        from deepface import DeepFace
        result = DeepFace.represent(
            img_path=img_path,
            model_name=_MODEL,
            enforce_detection=True,
            detector_backend="yunet",
        )
        if result:
            return result[0]["embedding"]
    except ValueError:
        pass  # 顔未検出（腕・背景のみ等）は正常系
    except Exception as e:
        print(f"  [スタッフ照合] エンベディング抽出失敗: {e}")
    return None


def recognize_staff(frame=None, image_bytes: bytes = None,
                    threshold: float = _THRESHOLD,
                    retry_threshold: float = _RETRY_THRESHOLD) -> Optional[dict]:
    """
    カメラフレームまたは画像バイト列からスタッフを照合する（ローカル）。

    Returns:
        {"id": int, "name": str, "department": str, "role": str, "distance": float}
        / {"retry": True, "distance": float} / None
    """
    import numpy as np

    if image_bytes is not None:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tf.write(image_bytes)
            img_path = tf.name
        owns_tmp = True
    elif frame is not None:
        try:
            import cv2
        except ImportError:
            return None
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            img_path = tf.name
        cv2.imwrite(img_path, frame)
        owns_tmp = True
    else:
        return None

    try:
        query_emb = _extract_embedding(img_path)
        if query_emb is None:
            return None
        query_emb = np.array(query_emb, dtype=float)
    finally:
        if owns_tmp:
            Path(img_path).unlink(missing_ok=True)

    candidates = _get_candidates()
    if not candidates:
        return None

    best = None
    best_dist = float("inf")

    for row in candidates:
        try:
            emb = np.array(json.loads(row["face_embedding"]), dtype=float)
            norm = np.linalg.norm(query_emb) * np.linalg.norm(emb) + 1e-9
            dist = float(1.0 - np.dot(query_emb, emb) / norm)
            if dist < best_dist:
                best_dist = dist
                best = row
        except Exception:
            continue

    ok = best and best_dist <= threshold
    near = not ok and best and best_dist <= retry_threshold
    similarity = max(0.0, (1.0 - best_dist) * 100) if best else 0.0
    if ok:
        verdict = "✅ 一致"
    elif near:
        verdict = f"🔄 リトライ候補 (distance={best_dist:.4f}, 類似度{similarity:.1f}%)"
    else:
        verdict = f"❌ 不一致 (distance={best_dist:.4f} が閾値 {threshold} を超えている)"
    print(f"  [スタッフ照合] {best['name'] if best else '?'}: 類似度 {similarity:.1f}%  →  {verdict}", flush=True)

    if ok:
        return {
            "id":         best["id"],
            "name":       best["name"],
            "department": best.get("department") or "",
            "role":       best.get("role") or "",
            "distance":   round(best_dist, 4),
        }
    if near:
        return {"retry": True, "distance": round(best_dist, 4)}
    return None
