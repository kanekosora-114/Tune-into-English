# app.py（ローカルHTTP/本番HTTPS 切替対応・完全版）
import os
import time
import logging
from typing import Optional

from flask import (
    Flask, redirect, request, session, url_for,
    render_template, jsonify
)
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheHandler

# タイムアウト・リトライ
import requests
from requests import Session
from requests.exceptions import ReadTimeout, ConnectionError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# URL解析（ログ用）
from urllib.parse import urlparse, parse_qs

# ==============================
# 設定・初期化
# ==============================
dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path)

APP_ENV = os.getenv("APP_ENV", "development")
IS_PROD = APP_ENV == "production"

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-secret")

# Cookie切替（ここが肝）
if IS_PROD:
    app.config.update(
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 7,  # 7日
        SESSION_COOKIE_NAME="tune_session",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="None",   # 本番はHTTPS前提
    )
else:
    app.config.update(
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 7,
        SESSION_COOKIE_NAME="tune_session",
        SESSION_COOKIE_SECURE=False,      # ローカルHTTP
        SESSION_COOKIE_SAMESITE="Lax",
    )

# ログ
logging.basicConfig(
    filename='error.log',
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s'
)
app.logger.addHandler(logging.StreamHandler())
app.logger.setLevel(logging.INFO)

# Spotify/OAuth 設定
CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:5000/callback")
SCOPE = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "user-read-currently-playing "
    "streaming user-read-email user-read-private"
)

app.logger.info(f"[ENV] APP_ENV={APP_ENV}")
app.logger.info(f"[OAuth] REDIRECT_URI={REDIRECT_URI}")

# ==============================
# OpenAI（行ごと翻訳）
# ==============================
from openai import OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ==============================
# Spotipy: セッションキャッシュ（Flask session保存）
# ==============================
class FlaskSessionCache(CacheHandler):
    def get_cached_token(self):
        return session.get("token_info")
    def save_token_to_cache(self, token_info):
        session["token_info"] = token_info
        return True

def get_sp_oauth(show_dialog: bool = True) -> SpotifyOAuth:
    # ローカルHTTPならOAuthlibのHTTPS強制を一時解除
    if REDIRECT_URI.startswith("http://"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,     # ダッシュボードと完全一致必須
        scope=SCOPE,
        cache_handler=FlaskSessionCache(),
        show_dialog=show_dialog,
        requests_timeout=15,
    )

# ==============================
# Spotipy セッション（タイムアウト & リトライ）
# ==============================
def make_spotify_client(token: str) -> spotipy.Spotify:
    session_s: Session = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session_s.mount("https://", adapter)
    session_s.mount("http://", adapter)
    return spotipy.Spotify(auth=token, requests_session=session_s, requests_timeout=(10, 20))

# ==============================
# トークン有効化ユーティリティ
# ==============================
_SKEW = 60  # 秒（早め更新）

def _token_valid(token_info: Optional[dict]) -> bool:
    if not token_info or "access_token" not in token_info:
        return False
    exp = int(token_info.get("expires_at", 0))
    return exp - _SKEW > int(time.time())

def ensure_token() -> Optional[str]:
    """有効なアクセストークンを返す。必要ならリフレッシュ。"""
    token_info = session.get("token_info")
    if _token_valid(token_info):
        return token_info["access_token"]

    if token_info and token_info.get("refresh_token"):
        try:
            sp_oauth = get_sp_oauth(show_dialog=False)
            new_info = sp_oauth.refresh_access_token(token_info["refresh_token"])
            now = int(time.time())
            new_info["expires_at"] = new_info.get("expires_at") or (now + int(new_info.get("expires_in", 3600)))
            if "refresh_token" not in new_info:
                new_info["refresh_token"] = token_info["refresh_token"]
            session["token_info"] = new_info
            return new_info["access_token"]
        except Exception:
            app.logger.exception("refresh_access_token failed")
            session.clear()
            return None
    return None

# ==============================
# キャッシュ系ヘッダ
# ==============================
@app.after_request
def add_no_store_headers(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

# ==============================
# ルーティング
# ==============================
@app.route('/')
def index():
    token = ensure_token()
    return render_template('login.html', access_token_present=bool(token))

@app.route('/player')
def player():
    token = ensure_token()
    if not token:
        return redirect(url_for('index'))
    sp = make_spotify_client(token)
    user_profile = sp.current_user()
    return render_template('player.html', access_token_present=True, access_token=token, user=user_profile)

@app.route('/mypage')
def mypage():
    token = ensure_token()
    if not token:
        return redirect(url_for('index'))
    sp = make_spotify_client(token)
    user_profile = sp.current_user()
    return render_template('mypage.html', user=user_profile, access_token_present=True)

@app.route('/login')
def login():
    sp_oauth = get_sp_oauth(show_dialog=True)
    auth_url = sp_oauth.get_authorize_url()
    q = parse_qs(urlparse(auth_url).query)
    app.logger.info(f"[AUTH_URL] {auth_url}")
    app.logger.info(f"[AUTH_URL] redirect_uri={q.get('redirect_uri',[None])[0]}")
    return redirect(auth_url)

@app.route('/callback')
def callback():
    code = request.args.get('code')
    if not code:
        error = request.args.get('error')
        return (f"Spotify認証が拒否されました: {error}" if error else "認証コードが見つかりませんでした。"), 400

    sp_oauth = get_sp_oauth(show_dialog=False)
    try:
        sp_oauth.get_access_token(code, as_dict=False)
        token_info = sp_oauth.get_cached_token()
        if not token_info or 'access_token' not in token_info:
            app.logger.error(f"get_cached_token 空/不正: {token_info}")
            return "認証に失敗しました（トークン取得に失敗）。", 500

        now = int(time.time())
        if not token_info.get('expires_at') and "expires_in" in token_info:
            token_info['expires_at'] = now + int(token_info["expires_in"])

        session.permanent = True
        session["token_info"] = token_info

        return redirect(url_for('player'))
    except Exception as e:
        app.logger.error(f"アクセストークン取得失敗: {e}", exc_info=True)
        session.clear()
        return "認証に失敗しました。", 500

@app.route('/get_access_token')
def get_access_token_for_frontend():
    token = ensure_token()
    if token:
        return {'access_token': token}
    return {'error': '認証情報が見つかりません'}, 401

@app.route('/transfer_playback', methods=['POST'])
def transfer_playback():
    token = ensure_token()
    if not token:
        return {'error': '未認証またはトークン期限切れ'}, 401

    data = request.get_json(silent=True) or {}
    device_id = data.get('device_id')
    if not device_id:
        return {'error': 'device_idが必要です'}, 400

    try:
        sp = make_spotify_client(token)
        sp.transfer_playback(device_id=device_id, force_play=False)
        return {'message': '再生デバイスを切り替えました'}, 200
    except spotipy.SpotifyException as e:
        if getattr(e, "http_status", None) == 401:
            session["token_info"] = None
            retry = ensure_token()
            if retry:
                try:
                    sp = make_spotify_client(retry)
                    sp.transfer_playback(device_id=device_id, force_play=False)
                    return {'message': '再生デバイスを切り替えました(リトライ)'}, 200
                except Exception as ee:
                    app.logger.error(f"デバイス切替リトライ失敗: {ee}", exc_info=True)
        app.logger.error(f"デバイス切替失敗: {e}", exc_info=True)
        return {'error': 'デバイス切り替えに失敗しました'}, 500
    except Exception as e:
        app.logger.error(f"デバイス切替失敗: {e}", exc_info=True)
        return {'error': 'デバイス切り替えに失敗しました'}, 500

@app.route('/play_track', methods=['POST'])
def play_track():
    token = ensure_token()
    if not token:
        return jsonify({'error': '未認証またはトークン期限切れ'}), 401

    d = request.get_json(silent=True) or {}
    track_uri = d.get('track_uri')
    preferred_device = d.get('device_id')  # 省略可
    if not track_uri:
        return jsonify({'error': 'track_uri が必要です'}), 400

    sp = make_spotify_client(token)

    def pick_device():
        """優先: 引数→active→先頭"""
        if preferred_device:
            return preferred_device
        devs = sp.devices().get("devices", [])
        if not devs:
            return None
        active = next((x for x in devs if x.get("is_active")), None)
        return (active or devs[0]).get("id")

    try:
        device_id = pick_device()
        if not device_id:
            return jsonify({'error': 'NO_ACTIVE_DEVICE',
                            'hint': 'Spotifyアプリを起動するか /player を開いてデバイス接続してください。'}), 409

        # 再生を奪わずに転送してから再生
        sp.transfer_playback(device_id=device_id, force_play=False)
        time.sleep(0.3)
        sp.start_playback(device_id=device_id, uris=[track_uri])
        return jsonify({'ok': True, 'device_id': device_id})
    except spotipy.SpotifyException as e:
        app.logger.exception("play_track failed")
        return jsonify({'error': getattr(e, "msg", str(e))}), 500
    except Exception as e:
        app.logger.exception("play_track failed (generic)")
        return jsonify({'error': str(e)}), 500


@app.get("/api/current-track")
def api_current_track():
    token = ensure_token()
    if not token:
        return {"ok": False, "note": "unauthorized or expired"}, 401
    try:
        sp = make_spotify_client(token)
        curr = sp.current_user_playing_track()
        if not curr or not curr.get("item"):
            return {"ok": False, "note": "no current track"}, 200

        item = curr["item"]
        artists = item.get("artists") or []
        album   = item.get("album") or {}
        images  = album.get("images") or []
        image_url = images[0]["url"] if images else ""

        return {
            "ok": True,
            "track_id": item.get("id"),
            "title": item.get("name"),
            "artist": (artists[0]["name"] if artists else ""),
            "album_art_url": image_url
        }, 200
    except (ReadTimeout, ConnectionError) as e:
        app.logger.warning(f"current-track timeout/network: {e}")
        return {"ok": False, "note": "timeout"}, 200
    except Exception as e:
        app.logger.error(f"現在再生取得エラー: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}, 500

# 歌詞取得（例：lrclib）
from lyrics_service import get_lyrics_by_title_artist

@app.get("/api/lyrics")
def api_lyrics():
    token = ensure_token()
    if not token:
        return {"ok": False, "note": "unauthorized or expired"}, 401
    try:
        sp = make_spotify_client(token)
        curr = sp.current_user_playing_track()
        if not curr or not curr.get("item"):
            return {"ok": False, "note": "no current track"}, 200
        item = curr["item"]
        title = item.get("name") or ""
        artists = item.get("artists") or []
        artist = artists[0]["name"] if artists else ""
        if not title:
            return {"ok": False, "note": "no title"}, 200

        lyrics = get_lyrics_by_title_artist(title, artist)
        if not lyrics:
            return {"ok": False, "note": "lyrics not found", "title": title, "artist": artist}, 200
        return {"ok": True, "title": title, "artist": artist, "lyrics": lyrics}, 200
    except (ReadTimeout, ConnectionError) as e:
        app.logger.warning(f"lyrics timeout/network: {e}")
        return {"ok": False, "note": "timeout"}, 200
    except Exception as e:
        app.logger.error(f"歌詞取得エラー: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}, 500

@app.get("/api/currently_playing")
def api_currently_playing():
    token = ensure_token()
    if not token:
        return {"is_playing": False, "note": "unauthorized or expired"}, 200

    def fetch():
        sp = make_spotify_client(ensure_token())
        return sp.current_user_playing_track()

    try:
        cur = fetch()
    except spotipy.SpotifyException as e:
        if getattr(e, "http_status", None) == 401:
            session["token_info"] = None
            if ensure_token():
                try:
                    cur = fetch()
                except Exception as ee:
                    app.logger.error(f"現在再生再試行も失敗: {ee}", exc_info=True)
                    return {"is_playing": False, "error": "retry failed"}, 200
            else:
                return {"is_playing": False, "note": "refresh failed"}, 200
        else:
            app.logger.error(f"現在再生取得エラー: {e}", exc_info=True)
            return {"is_playing": False, "error": str(e)}, 200
    except (ReadTimeout, ConnectionError) as e:
        app.logger.warning(f"currently_playing timeout/network: {e}")
        return {"is_playing": False, "note": "timeout"}, 200
    except Exception as e:
        app.logger.error(f"現在再生取得エラー: {e}", exc_info=True)
        return {"is_playing": False, "error": str(e)}, 200

    if not cur or not cur.get("is_playing"):
        return {"is_playing": False}, 200

    item = cur.get("item") or {}
    artists = item.get("artists") or []
    album = item.get("album") or {}
    images = album.get("images") or []
    image_url = images[0]["url"] if images else ""

    return {
        "is_playing": True,
        "title": item.get("name") or "",
        "artist": (artists[0]["name"] if artists else ""),
        "track_id": item.get("id"),
        "album_art_url": image_url,
        "duration_ms": item.get("duration_ms") or 0,
        "progress_ms": cur.get("progress_ms") or 0,
        "timestamp_ms": int(time.time() * 1000)
    }, 200

@app.post("/api/translate_lines")
def api_translate_lines():
    try:
        if openai_client is None:
            return {"ok": False, "error": "OPENAI_API_KEY not set"}, 400

        data = request.get_json(silent=True) or {}
        lines = data.get("lines") or []
        if not isinstance(lines, list) or not lines:
            return {"ok": False, "error": "lines required"}, 400

        out, chunk = [], []

        def flush():
            if not chunk:
                return
            prompt = (
                "以下の歌詞行を自然な日本語に、行数を変えず同じ行数で訳してください。\n"
                "出力は訳文のみ。番号や解説は付けないでください。\n\n"
                + "\n".join(chunk)
            )
            resp = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a professional translator."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
            )
            jp = (resp.choices[0].message.content or "").splitlines()
            if len(jp) < len(chunk):
                jp += [""] * (len(chunk) - len(jp))
            out.extend(jp[: len(chunk)])
            chunk.clear()

        for s in lines:
            chunk.append(s if str(s).strip() else "(空行)")
            if len(chunk) >= 8:
                flush()
        flush()

        return {"ok": True, "jp": out}, 200
    except Exception as e:
        app.logger.error(f"/api/translate_lines error: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}, 500

@app.get("/api/search_tracks")
def api_search_tracks():
    token = ensure_token()
    if not token:
        return jsonify({"items": [], "next_offset": None, "note": "unauthorized"}), 401

    q = (request.args.get("q") or "").strip()
    limit = request.args.get("limit", default=12, type=int)
    offset = request.args.get("offset", default=0, type=int)
    if not q:
        return jsonify({"items": [], "next_offset": None})

    try:
        sp = make_spotify_client(token)
        try:
            me = sp.current_user()
            market = me.get("country") or None
        except Exception:
            market = None

        resp = sp.search(q=q, type="track", limit=limit, offset=offset, market=market)
        tracks = resp.get("tracks", {})
        items = []
        for t in tracks.get("items", []):
            artists = ", ".join([a["name"] for a in t.get("artists", [])])
            album = t.get("album", {})
            img = album["images"][-1]["url"] if album.get("images") else ""
            items.append({
                "id": t.get("id"),
                "name": t.get("name"),
                "artists": artists,
                "album": album.get("name"),
                "image": img,
                "uri": t.get("uri"),
                "duration_ms": t.get("duration_ms"),
            })

        total = tracks.get("total", 0)
        next_offset = (offset + limit) if (offset + limit) < total else None
        return jsonify({"items": items, "next_offset": next_offset})
    except Exception as e:
        app.logger.exception("search error")
        return jsonify({"error": str(e)}), 500

@app.post("/api/queue_track")
def api_queue_track():
    token = ensure_token()
    if not token:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    uri = data.get("uri")
    preferred_device = data.get("device_id")
    if not uri:
        return jsonify({"error": "uri required"}), 400

    sp = make_spotify_client(token)

    try:
        sp.add_to_queue(uri)
        return jsonify({"ok": True})
    except spotipy.SpotifyException as e:
        if getattr(e, "http_status", None) == 404:
            try:
                device_id = preferred_device
                if not device_id:
                    devs = sp.devices().get("devices", [])
                    if devs:
                        active = next((d for d in devs if d.get("is_active")), None)
                        device_id = (active or devs[0]).get("id")
                if not device_id:
                    return jsonify({
                        "error": "NO_ACTIVE_DEVICE",
                        "hint": "Spotifyアプリを起動するか、プレイヤーページを開いてデバイス接続してください。"
                    }), 409

                sp.transfer_playback(device_id=device_id, force_play=False)
                time.sleep(0.4)
                sp.add_to_queue(uri)
                return jsonify({"ok": True, "activated_device": device_id})
            except Exception as ee:
                app.logger.exception("queue retry after transfer failed")
                return jsonify({"error": "queue_failed_after_transfer", "detail": str(ee)}), 500
        app.logger.exception("queue error")
        return jsonify({"error": getattr(e, "msg", str(e))}), 500
    except Exception as e:
        app.logger.exception("queue error")
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return "ok", 200

# ==============================
# エントリーポイント
# ==============================
if __name__ == '__main__':
    os.makedirs('templates', exist_ok=True)
    host = "0.0.0.0" if IS_PROD else "127.0.0.1"
    app.run(host=host, port=5000, debug=not IS_PROD)
