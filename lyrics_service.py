# lyrics_service.py
import math
import requests

BASE = "https://lrclib.net/api"

def _get(path: str, params: dict):
    """LRCLIB APIを叩くヘルパー"""
    r = requests.get(f"{BASE}{path}", params=params, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()
import re
import requests
from typing import Optional

LRCLIB_ENDPOINT = "https://lrclib.net/api/get"

DASHES = r"[–—-]"
FEAT_PAT = re.compile(r"\s*(?:feat\.?|ft\.?)\s+.*$", re.IGNORECASE)
SUFFIX_PAT = re.compile(
    rf"\s*{DASHES}\s*(?:live|remix|radio edit|edit|version|remaster(?:ed)?\s*\d{{2,4}}?|remaster(?:ed)?|explicit|clean)\s*$",
    re.IGNORECASE,
)

def normalize_title(title: str) -> str:
    t = (title or "").strip()
    t = re.sub(r"\s*[\(\[\{].*?[\)\]\}]\s*", " ", t)           # ()[]{} の付加情報
    t = re.sub(SUFFIX_PAT, "", t)                              # ダッシュ以降のバージョン表記
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"\s+", " ", t).strip()
    return t

def normalize_artist(artist: str) -> str:
    a = FEAT_PAT.sub("", artist or "").strip()
    a = re.sub(r"\s+", " ", a)
    return a

def get_lyrics_by_title_artist(title: str, artist: str) -> Optional[str]:
    """
    タイトル＋アーティストで歌詞取得（LRCLIB使用／トークン不要）
    - 同期歌詞があればLRC形式に整形
    - なければ通常歌詞（plainLyrics）
    """
    t = normalize_title(title or "")
    a = normalize_artist(artist or "")
    if not t:
        return None

    try:
        # まず厳密寄り
        params = {"track_name": t, "artist_name": a}
        r = requests.get(LRCLIB_ENDPOINT, params=params, timeout=8)
        if r.status_code == 200:
            data = r.json()
            # 1) syncedLyrics → LRCとして返す
            if isinstance(data, dict) and (data.get("syncedLyrics") or "").strip():
                return (data["syncedLyrics"] or "").strip()
            # 2) plainLyrics
            if isinstance(data, dict) and (data.get("plainLyrics") or "").strip():
                return (data["plainLyrics"] or "").strip()

        # タイトル正規化前でもう一度（保険）
        if t != (title or "").strip():
            params = {"track_name": (title or "").strip(), "artist_name": a}
            r = requests.get(LRCLIB_ENDPOINT, params=params, timeout=8)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and (data.get("syncedLyrics") or "").strip():
                    return (data["syncedLyrics"] or "").strip()
                if isinstance(data, dict) and (data.get("plainLyrics") or "").strip():
                    return (data["plainLyrics"] or "").strip()
    except Exception:
        pass

    return None

def _seconds(ms: int | None) -> int | None:
    """Spotifyのduration_msを秒に変換"""
    if not ms:
        return None
    return max(1, math.floor(ms / 1000))

def _pick_best(candidates: list, title: str, artist: str, dur_sec: int | None):
    """候補の中から最も近いものをスコアリングして選ぶ"""
    if not candidates:
        return None

    def score(c):
        s = 0
        if title and c.get("trackName", "").lower() == title.lower():
            s += 3
        if artist and c.get("artistName", "").lower() == artist.lower():
            s += 3
        cdur = c.get("duration")
        if dur_sec and cdur:
            diff = abs(cdur - dur_sec)
            if diff <= 2:
                s += 2
            elif diff <= 5:
                s += 1
        return s

    return sorted(candidates, key=score, reverse=True)[0]

def get_lyrics_by_title_artist(
    title: str,
    artist: str,
    album: str | None = None,
    duration_ms: int | None = None,
    isrc: str | None = None
) -> str | None:
    """
    LRCLIBから同期歌詞(LRC)を優先して取得。無ければプレーン歌詞。
    見つからなければ None を返す。
    """
    # 1) メタデータで直接取得
    params = {"track_name": title, "artist_name": artist}
    if album:
        params["album_name"] = album
    if isrc:
        params["isrc"] = isrc
    dur_sec = _seconds(duration_ms)
    if dur_sec:
        params["duration"] = dur_sec

    data = _get("/get", params)
    if data:
        if data.get("syncedLyrics"):
            return data["syncedLyrics"]
        if data.get("plainLyrics"):
            return data["plainLyrics"]

    # 2) 検索で候補から選ぶ
    q = " ".join(x for x in [title, artist, album] if x)
    cands = _get("/search", {"q": q}) or []
    best = _pick_best(cands, title, artist, dur_sec)
    if best:
        if best.get("syncedLyrics"):
            return best["syncedLyrics"]
        if best.get("plainLyrics"):
            return best["plainLyrics"]

    return None
