# app.py
import os
import time
import logging
from flask import Flask, redirect, request, session, url_for, render_template
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from lyrics_service import get_lyrics_by_title_artist

# タイムアウト・リトライ用
import requests
from requests import Session
from requests.exceptions import ReadTimeout, ConnectionError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------
# 設定・初期化
# ---------------------------------------
dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path)

app = Flask(__name__)
# ★ 固定の secret_key を使う（.env で設定可能）
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-secret")
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 7  # 7日間

# ログ設定
logging.basicConfig(
    filename='error.log',
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s:%(message)s'
)
app.logger.addHandler(logging.StreamHandler())

CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")
SCOPE = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "user-read-currently-playing "
    "streaming user-read-email user-read-private"
)

# ---------------------------------------
# OpenAI（行ごと翻訳）
# ---------------------------------------
from openai import OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ---------------------------------------
# Spotipy セッション（タイムアウト & リトライ）作成
# ---------------------------------------
def make_spotify_client(token: str) -> spotipy.Spotify:
    """
    タイムアウト & リトライ設定済みの Spotipy クライアントを作る
    - 接続:10秒 / 読み取り:20秒
    - 429/5xx を指数バックオフで最大3回リトライ
    """
    session: Session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.6,  # 0.6, 1.2, 2.4...
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return spotipy.Spotify(
        auth=token,
        requests_session=session,
        requests_timeout=(10, 20)  # (connect, read)
    )

# ---------------------------------------
# 共通処理: 有効なトークンを確実に返す
# ---------------------------------------
def ensure_token():
    """有効なアクセストークンを返す。期限切れならリフレッシュ"""
    access_token  = session.get("access_token")
    refresh_token = session.get("refresh_token")
    expires_at    = int(session.get("expires_at", 0))

    # 60秒の余裕を見て「もうすぐ切れる」も更新対象にする
    SKEW = 60
    now = int(time.time())

    if not access_token:
        return None
    if expires_at - SKEW > now:
        return access_token

    if refresh_token:
        try:
            sp_oauth = SpotifyOAuth(
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                redirect_uri=REDIRECT_URI,
                scope=SCOPE
            )
            token_info = sp_oauth.refresh_access_token(refresh_token)
            # expires_at が無い実装差へのフォールバック
            new_expires_at = token_info.get("expires_at")
            if not new_expires_at and "expires_in" in token_info:
                new_expires_at = now + int(token_info["expires_in"])
            session["access_token"]  = token_info["access_token"]
            session["expires_at"]    = new_expires_at or (now + 3000)
            session["refresh_token"] = token_info.get("refresh_token", refresh_token)
            return session["access_token"]
        except Exception:
            app.logger.exception("refresh_access_token failed")
            session.clear()
            return None
    return None

# ---------------------------------------
# ルーティング
# ---------------------------------------

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
    return render_template('player.html', access_token_present=True,
                           access_token=token, user=user_profile)

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
    sp_oauth = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        show_dialog=False  # refresh_token が確保できたら False 推奨
    )
    return redirect(sp_oauth.get_authorize_url())

@app.route('/callback')
def callback():
    code = request.args.get('code')
    if not code:
        error = request.args.get('error')
        return (f"Spotify認証が拒否されました: {error}" if error else "認証コードが見つかりませんでした。"), 400

    sp_oauth = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE
    )
    try:
        # ★ DeprecationWarning回避：返り値は使わずキャッシュ化だけ
        sp_oauth.get_access_token(code, as_dict=False)

        # ★ 常に dict で取得
        token_info = sp_oauth.get_cached_token()
        if not token_info or 'access_token' not in token_info:
            app.logger.error(f"get_cached_token が空 or 不正: {token_info}")
            return "認証に失敗しました（トークン取得に失敗）。", 500

        now = int(time.time())
        expires_at = token_info.get('expires_at')
        if not expires_at and "expires_in" in token_info:
            expires_at = now + int(token_info["expires_in"])

        session.permanent = True
        session['access_token']  = token_info['access_token']
        session['refresh_token'] = token_info.get('refresh_token')  # 取れたかログで確認推奨
        session['expires_at']    = expires_at or (now + 3000)

        return redirect(url_for('player'))
    except Exception as e:
        app.logger.error(f"アクセストークンの取得に失敗: {e}", exc_info=True)
        session.clear()
        return "認証に失敗しました。", 500

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
        # 401 が来たら一度だけ再試行
        if getattr(e, "http_status", None) == 401:
            session["access_token"] = None
            retry_token = ensure_token()
            if retry_token:
                try:
                    sp = make_spotify_client(retry_token)
                    sp.transfer_playback(device_id=device_id, force_play=False)
                    return {'message': '再生デバイスを切り替えました(リトライ)'}, 200
                except Exception as ee:
                    app.logger.error(f"デバイス切り替えリトライ失敗: {ee}", exc_info=True)
        app.logger.error(f"デバイス切り替え失敗: {e}", exc_info=True)
        return {'error': 'デバイス切り替えに失敗しました'}, 500
    except Exception as e:
        app.logger.error(f"デバイス切り替え失敗: {e}", exc_info=True)
        return {'error': 'デバイス切り替えに失敗しました'}, 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/get_access_token')
def get_access_token_for_frontend():
    token = ensure_token()
    if token:
        return {'access_token': token}
    return {'error': '認証情報が見つかりません'}, 401

@app.route('/play_track', methods=['POST'])
def play_track():
    token = ensure_token()
    if not token:
        return {'error': '未認証またはトークン期限切れ'}, 401

    data = request.get_json(silent=True) or {}
    track_uri = data.get('track_uri')
    device_id = data.get('device_id')
    if not track_uri or not device_id:
        return {'error': 'track_uriとdevice_idが必要です'}, 400

    try:
        sp = make_spotify_client(token)
        sp.start_playback(device_id=device_id, uris=[track_uri])
        return {'message': '再生開始'}, 200
    except spotipy.SpotifyException as e:
        if getattr(e, "http_status", None) == 401:
            # 一度だけリフレッシュして再試行
            session["access_token"] = None
            retry_token = ensure_token()
            if retry_token:
                try:
                    sp = make_spotify_client(retry_token)
                    sp.start_playback(device_id=device_id, uris=[track_uri])
                    return {'message': '再生開始(リトライ)'}, 200
                except Exception as ee:
                    app.logger.error(f"再生リトライ失敗: {ee}", exc_info=True)
        app.logger.error(f"Spotify APIエラー: {e}", exc_info=True)
        return {'error': f'Spotify APIエラー: {getattr(e, "msg", str(e))}'}, 500
    except (ReadTimeout, ConnectionError) as e:
        app.logger.warning(f"start_playback timeout/network: {e}")
        return {'error': '一時的なネットワーク問題（後で再試行）'}, 503
    except Exception as e:
        app.logger.error(f"再生失敗: {e}", exc_info=True)
        return {'error': '予期せぬエラーが発生しました'}, 500

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

@app.route("/health")
def health():
    return "ok", 200

@app.get("/api/currently_playing")
def api_currently_playing():
    """
    今の曲名・アーティスト名・再生位置(ms)・アルバムアートを返す最小API
    """
    token = ensure_token()
    if not token:
        return {"is_playing": False, "note": "unauthorized or expired"}, 200

    def fetch():
        sp = make_spotify_client(ensure_token())
        return sp.current_user_playing_track()

    try:
        cur = fetch()
    except spotipy.SpotifyException as e:
        # 401 なら一度だけ自動リフレッシュして再試行
        if getattr(e, "http_status", None) == 401:
            session["access_token"] = None
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

# ---------------------------------------
# 行ごと翻訳 API
# ---------------------------------------
@app.post("/api/translate_lines")
def api_translate_lines():
    """
    JSON: { "lines": ["text1","text2", ...] }
     →   { "ok": true, "jp": ["訳1","訳2", ...] }
    """
    try:
        if openai_client is None:
            return {"ok": False, "error": "OPENAI_API_KEY not set"}, 400

        data = request.get_json(silent=True) or {}
        lines = data.get("lines") or []
        if not isinstance(lines, list) or not lines:
            return {"ok": False, "error": "lines required"}, 400

        out = []
        chunk = []

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
            if len(chunk) >= 8:  # 小分けで安定
                flush()
        flush()

        return {"ok": True, "jp": out}, 200
    except Exception as e:
        app.logger.error(f"/api/translate_lines error: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}, 500

# ---------------------------------------
# エントリーポイント
# ---------------------------------------
if __name__ == '__main__':
    os.makedirs('templates', exist_ok=True)
    app.run(debug=True)
