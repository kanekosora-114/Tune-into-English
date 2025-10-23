# lyrics_service.py
import os
import re
import math
import requests
from typing import Optional, List

# ---------- LRCLIB ----------
BASE = "https://lrclib.net/api"

def _get(path: str, params: dict):
    r = requests.get(f"{BASE}{path}", params=params, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

def _seconds(ms: int | None) -> int | None:
    if not ms:
        return None
    return max(1, math.floor(ms / 1000))

def _pick_best(candidates: list, title: str, artist: str, dur_sec: int | None):
    if not candidates:
        return None
    def score(c):
        s = 0
        if title and c.get("trackName", "").lower() == (title or "").lower():
            s += 3
        if artist and c.get("artistName", "").lower() == (artist or "").lower():
            s += 3
        cdur = c.get("duration")
        if dur_sec and cdur:
            diff = abs(cdur - dur_sec)
            if diff <= 2: s += 2
            elif diff <= 5: s += 1
        return s
    return sorted(candidates, key=score, reverse=True)[0]

def get_lyrics_by_title_artist(
    title: str,
    artist: str,
    album: str | None = None,
    duration_ms: int | None = None,
    isrc: str | None = None
) -> Optional[str]:
    """
    LRCLIBから同期歌詞(LRC)を優先して取得。無ければプレーン歌詞。
    見つからなければ None。
    """
    if not title or not artist:
        return None

    params = {"track_name": title, "artist_name": artist}
    if album: params["album_name"] = album
    if isrc: params["isrc"] = isrc
    dur_sec = _seconds(duration_ms)
    if dur_sec: params["duration"] = dur_sec

    try:
        data = _get("/get", params)
        if data:
            if data.get("syncedLyrics"): return data["syncedLyrics"].strip()
            if data.get("plainLyrics"):  return data["plainLyrics"].strip()

        # 見つからなければ検索
        q = " ".join(x for x in [title, artist, album] if x)
        cands = _get("/search", {"q": q}) or []
        best = _pick_best(cands, title, artist, dur_sec)
        if best:
            if best.get("syncedLyrics"): return best["syncedLyrics"].strip()
            if best.get("plainLyrics"):  return best["plainLyrics"].strip()
    except Exception:
        pass

    return None

# ---------- 翻訳（OpenAI Responses API） ----------
# pip install openai >= 1.0 が必要
from openai import OpenAI
_client = None

def _client_once() -> OpenAI:
    global _client
    if _client is None:
        # OPENAI_API_KEY は環境変数に設定しておく
        _client = OpenAI()  # 自動で os.environ['OPENAI_API_KEY'] を読む
    return _client

_TIME_TAG = re.compile(r"^(\s*(?:\[\d{1,2}:\d{2}(?:\.\d{1,3})?\])+)\s*(.*)$")

def _split_lrc_lines(text: str) -> List[str]:
    return text.splitlines()

def _needs_timestamp_preserve(text: str) -> bool:
    # 先頭数行にタイムタグがあればLRCとみなす
    lines = text.splitlines()
    for ln in lines[:10]:
        if _TIME_TAG.match(ln):
            return True
    return False

def translate_lyrics(
    lyrics: str,
    target_lang: str = "ja",
    model: str = "gpt-4o-mini",
) -> str:
    """
    歌詞を target_lang に翻訳。
    - LRC形式なら [mm:ss.xx] のタイムタグは保持して本文のみ翻訳
    - プレーン歌詞はそのまま行単位で翻訳
    """
    if not lyrics:
        return ""

    preserve = _needs_timestamp_preserve(lyrics)
    system = (
        "You are a professional lyric translator. "
        f"Translate the user's lyrics into {target_lang}. "
        "Preserve line breaks exactly. "
        "If a line starts with one or more time tags like [mm:ss.xx], "
        "keep the tags unchanged and translate only the following text. "
        "Do not add explanations or parentheses. Return only the translated lyrics."
    )
    client = _client_once()

    # 長文対策：4,000文字ごとに分割
    chunks = []
    buf = []
    size = 0
    for line in _split_lrc_lines(lyrics):
        ln = line + "\n"
        if size + len(ln) > 4000 and buf:
            chunks.append("".join(buf))
            buf, size = [ln], len(ln)
        else:
            buf.append(ln)
            size += len(ln)
    if buf:
        chunks.append("".join(buf))

    out_parts = []
    for chunk in chunks:
        resp = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": chunk},
            ],
        )
        out_parts.append(resp.output_text.strip())

    return "\n".join(out_parts).strip()
