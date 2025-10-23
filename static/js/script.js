/* ===================== グローバル状態 ===================== */
let currentDeviceId = null;
let player = null;                 // Web Playback SDK インスタンス
let currentPlaybackState = null;   // SDKのstate

// 再生モデル：UIは常にこれを描画
const nowPlaying = {
  title: "曲名",
  artist: "アーティスト名",
  albumArt: "",
  durationMs: 0,
  baseProgressMs: 0,   // 基準時点での再生位置(ms)
  baseTimestampMs: 0,  // 基準時刻(Date.now)
  isPlaying: false,
};

/* ===================== DOM refs（一度だけ） ===================== */
const leftPanel             = document.getElementById('leftPanel');
const toggleLeftPanelButton = document.getElementById('toggleLeftPanelButton');

const mainPlayPauseIcon   = document.getElementById('mainPlayPauseIcon');
const footerPlayPauseIcon = document.getElementById('footerPlayPauseIcon');

const prevTrackButton        = document.getElementById('prevTrackButton');
const togglePlayButton       = document.getElementById('togglePlayButton');
const nextTrackButton        = document.getElementById('nextTrackButton');
const footerPrevTrackButton  = document.getElementById('footerPrevTrackButton');
const footerTogglePlayButton = document.getElementById('footerTogglePlayButton');
const footerNextTrackButton  = document.getElementById('footerNextTrackButton');

const seekBar          = document.getElementById('seekBar');
const currentTimeLabel = document.getElementById('currentTime');
const totalTimeLabel   = document.getElementById('totalTime');

const volumeSlider = document.getElementById('volumeSlider');

const titleElem   = document.getElementById('current-song-title');
const artistElem  = document.getElementById('current-artist-name');
const artElem     = document.getElementById('album-art');

const footerArt    = document.getElementById('footerAlbumArt');
const footerTitle  = document.getElementById('footerSongTitle');
const footerArtist = document.getElementById('footerArtistName');

// 歌詞
const $status  = document.getElementById('lyrics-status');
const $content = document.getElementById('lyrics-content');

// 翻訳トグル
const translateToggle = document.getElementById('translateToggle');
let translateEnabled = null;

// 起動ボタン（モバイルの音声制限対策）
const startBtn = document.getElementById('start-player');

/* ===================== 定数 / ユーティリティ ===================== */
const PLACE_MAIN = "https://placehold.co/220x220/121212/ffffff?text=No+Album+Art";
const PLACE_FOOT = "https://placehold.co/60x60/282828/ffffff?text=Art";

const toMMSS = (ms) => {
  if (!Number.isFinite(ms)) return "0:00";
  const s = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(s / 60);
  const ss = String(s % 60).padStart(2, "0");
  return `${m}:${ss}`;
};

async function safeFetchJson(url, init) {
  try {
    const res = await fetch(url, { cache: "no-store", ...(init || {}) });
    const ct = (res.headers.get("content-type") || "").toLowerCase();
    if (!ct.includes("application/json")) return null;
    return await res.json();
  } catch { return null; }
}

/* ============ アクセストークン（テンプレ埋め込み or API） ============ */
async function getAccessToken() {
  if (typeof SPOTIFY_ACCESS_TOKEN === "string" && SPOTIFY_ACCESS_TOKEN.length > 10) {
    return SPOTIFY_ACCESS_TOKEN;
  }
  const j = await safeFetchJson("/get_access_token");
  if (j && j.access_token) return j.access_token;
  throw new Error("access token missing");
}

/* ===================== モデル→UI描画 ===================== */
function setPlayIcons(isPlaying) {
  const playPNG  = "/static/images/play.png";
  const pausePNG = "/static/images/pause.png"; // pause.png が必須（置いてね）

  const icon = isPlaying ? pausePNG : playPNG;
  const alt  = isPlaying ? "一時停止" : "再生";

  if (mainPlayPauseIcon) {
    mainPlayPauseIcon.src = icon;
    mainPlayPauseIcon.alt = alt;
  }
  if (footerPlayPauseIcon) {
    footerPlayPauseIcon.src = icon;
    footerPlayPauseIcon.alt = alt;
  }
}

function renderProgressFromModel() {
  const { durationMs, baseProgressMs, baseTimestampMs, isPlaying } = nowPlaying;

  if (!durationMs) {
    if (seekBar) seekBar.value = 0;
    if (currentTimeLabel) currentTimeLabel.textContent = "0:00";
    if (totalTimeLabel)   totalTimeLabel.textContent   = "0:00";
    return;
  }

  let prog = baseProgressMs;
  if (isPlaying) {
    const dt = Date.now() - baseTimestampMs;
    prog = Math.min(durationMs, Math.max(0, baseProgressMs + dt));
  }

  if (seekBar) {
    const pct = Math.min(100, Math.max(0, (prog / durationMs) * 100));
    seekBar.value = Math.round(pct);
  }
  if (currentTimeLabel) currentTimeLabel.textContent = toMMSS(prog);
  if (totalTimeLabel)   totalTimeLabel.textContent   = toMMSS(durationMs);

  highlightByTime(prog / 1000);
}

function applyMetaToUI() {
  if (titleElem)   titleElem.textContent   = nowPlaying.title  || "曲名";
  if (artistElem)  artistElem.textContent  = nowPlaying.artist || "アーティスト名";
  if (footerTitle) footerTitle.textContent = nowPlaying.title  || "曲名";
  if (footerArtist)footerArtist.textContent= nowPlaying.artist || "アーティスト名";

  const art = nowPlaying.albumArt;
  if (artElem)   artElem.src   = art || PLACE_MAIN;
  if (footerArt) footerArt.src = art || PLACE_FOOT;

  setPlayIcons(nowPlaying.isPlaying);
  renderProgressFromModel();
}

/* ===================== モデル更新（API / SDK） ===================== */
function setModelFromApi(d) {
  nowPlaying.title          = d.title || "曲名";
  nowPlaying.artist         = d.artist || "アーティスト名";
  nowPlaying.albumArt       = d.album_art_url || "";
  nowPlaying.durationMs     = Number(d.duration_ms) || 0;
  nowPlaying.baseProgressMs = Number(d.progress_ms) || 0;
  nowPlaying.baseTimestampMs= Date.now();
  nowPlaying.isPlaying      = !!d.is_playing;
  applyMetaToUI();
}

function setModelFromSDK(state) {
  if (!state) return;
  nowPlaying.baseProgressMs  = Number(state.position) || 0;
  nowPlaying.baseTimestampMs = Date.now();
  nowPlaying.isPlaying       = !state.paused;
  applyMetaToUI();
}

async function reconcileFromApi() {
  const d = await safeFetchJson('/api/currently_playing');
  if (!d) return;
  if (!d.is_playing) {
    nowPlaying.isPlaying = false;
    nowPlaying.baseTimestampMs = Date.now();
    applyMetaToUI();
    return;
  }
  setModelFromApi(d);
}

/* ===================== rAF ティッカー ===================== */
let rafId = null;
function startTicker() {
  if (rafId) return;
  const tick = () => {
    rafId = requestAnimationFrame(tick);
    renderProgressFromModel();
  };
  rafId = requestAnimationFrame(tick);
}
function stopTicker() {
  if (rafId) cancelAnimationFrame(rafId);
  rafId = null;
}

/* ===================== 歌詞 ===================== */
let parsedLyrics = [];   // [{ t:秒, text:行 }]
let currentLyricIndex = -1;
let lastTrackId = null;

function parseLRC(lrcText) {
  const out = [];
  if (!lrcText) return out;
  const re = /\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}|\d{1,2}))?\](.*)/g;
  for (const raw of lrcText.split(/\r?\n/)) {
    let m;
    while ((m = re.exec(raw)) !== null) {
      const min = parseInt(m[1], 10) || 0;
      const sec = parseInt(m[2], 10) || 0;
      const frac = (m[3] || "0");
      const ms = parseInt(frac.length === 2 ? frac + "0" : frac, 10) || 0;
      const t = (min * 60 + sec) + (ms / 1000);
      out.push({ t, text: (m[4] || "").trim() });
    }
  }
  return out.sort((a, b) => a.t - b.t);
}

function renderLyrics(lines) {
  if (!$content) return;
  $content.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (const l of lines) {
    const row = document.createElement("div");
    row.className = "lyric-line";

    const orig = document.createElement("div");
    orig.className = "lyric-orig";
    orig.textContent = l.text || "";

    const trans = document.createElement("div");
    trans.className = "lyric-trans";
    trans.textContent = "";

    row.appendChild(orig);
    row.appendChild(trans);
    frag.appendChild(row);
  }
  $content.appendChild(frag);
  applyTranslateVisibility();
  currentLyricIndex = -1;
}

function highlightByTime(currentSec) {
  if (!parsedLyrics.length || !$content) return;
  let idx = currentLyricIndex;
  if (idx < 0 || idx >= parsedLyrics.length || currentSec < parsedLyrics[idx].t) idx = -1;
  for (let i = Math.max(0, idx); i < parsedLyrics.length; i++) {
    if (currentSec >= parsedLyrics[i].t) idx = i;
    else break;
  }
  if (idx !== -1 && idx !== currentLyricIndex) {
    const rows = $content.getElementsByClassName("lyric-line");
    if (currentLyricIndex >= 0 && rows[currentLyricIndex]) rows[currentLyricIndex].classList.remove("active");
    if (rows[idx]) {
      rows[idx].classList.add("active");
      rows[idx].scrollIntoView({ behavior: "smooth", block: "center" });
    }
    currentLyricIndex = idx;
  }
}

function setStatus(msg){ if ($status) $status.textContent = msg || ""; }
function setLyricsPlain(txt){ if ($content) $content.innerHTML = (txt || "").replace(/\n/g, "<br>"); }
function applyTranslateVisibility() {
  if (!$content) return;
  if (translateEnabled) $content.classList.remove('hide-trans');
  else $content.classList.add('hide-trans');
}

async function fetchCurrentTrack() {
  return (await safeFetchJson('/api/current-track')) || { ok: false };
}
async function fetchTimedLyrics() {
  return await safeFetchJson('/api/lyrics_timed'); // サーバ側にある場合のみ
}
async function fetchPlainLyrics() {
  const j = await safeFetchJson('/api/lyrics');
  return j || { ok: false };
}

async function translateParsedLyrics() {
  if (!parsedLyrics.length || !translateEnabled) return;
  const lines = parsedLyrics.map(l => l.text || "");
  try {
    const res = await fetch("/api/translate_lines", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lines })
    });
    const data = await res.json();
    if (!data.ok || !Array.isArray(data.jp)) return;
    const jp = data.jp;
    const rows = $content.getElementsByClassName("lyric-line");
    for (let i = 0; i < Math.min(rows.length, jp.length); i++) {
      const transEl = rows[i].querySelector(".lyric-trans");
      if (transEl && !transEl.textContent) transEl.textContent = jp[i] || "";
    }
  } catch {}
}

async function loadLyricsOnce() {
  try {
    setStatus('読み込み中…');
    parsedLyrics = [];
    currentLyricIndex = -1;
    setLyricsPlain('');

    const meta = await fetchCurrentTrack();
    if (!meta.ok || !meta.track_id) {
      setStatus('再生中の曲が見つかりません。Spotifyで再生してから更新してください。');
      return;
    }
    lastTrackId = meta.track_id;

    // 1) timed
    const timedData = await fetchTimedLyrics();
    if (timedData && timedData.ok && Array.isArray(timedData.timed) && timedData.timed.length) {
      parsedLyrics = timedData.timed.map(([ms, text]) => ({ t: (ms || 0) / 1000, text: text || "" }));
      renderLyrics(parsedLyrics);
      if (translateEnabled) translateParsedLyrics();
      if (currentPlaybackState) highlightByTime((currentPlaybackState.position || 0) / 1000);
      setStatus(`${timedData.title} — ${timedData.artist}${timedData.synced ? '' : '（擬似同期）'}`);
      return;
    }

    // 2) plain
    const data = await fetchPlainLyrics();
    if ((data.ok === undefined || data.ok === true) && typeof data.lyrics === "string" && data.lyrics.length) {
      setStatus(`${data.title || meta.title} — ${data.artist || meta.artist}`);
      const maybeLrc = data.lyrics;
      const parsed = parseLRC(maybeLrc);
      if (parsed.length) {
        parsedLyrics = parsed;
        renderLyrics(parsedLyrics);
        if (translateEnabled) translateParsedLyrics();
        if (currentPlaybackState) highlightByTime((currentPlaybackState.position || 0) / 1000);
      } else {
        setLyricsPlain(maybeLrc);
      }
      return;
    }

    setStatus(`${meta.title} — ${meta.artist}`);
    setLyricsPlain('歌詞が見つかりませんでした。');
  } catch {
    setStatus('取得中にエラーが発生しました。');
  }
}

async function pollTrackChange() {
  try {
    const meta = await fetchCurrentTrack();
    if (meta.ok && meta.track_id && meta.track_id !== lastTrackId) {
      await loadLyricsOnce();
    }
  } catch {}
}

/* ===================== コントロール紐付け ===================== */
function bindControls() {
  const clickWrap = (fn) => async () => {
    if (!player) return;
    try { await fn(); } catch {}
    const state = await player.getCurrentState().catch(()=>null);
    if (state) setModelFromSDK(state);
    reconcileFromApi();
  };
  const bind = (el, fn) => { if (el) el.addEventListener('click', fn); };

  bind(togglePlayButton,       clickWrap(()=>player.togglePlay()));
  bind(footerTogglePlayButton, clickWrap(()=>player.togglePlay()));
  bind(prevTrackButton,        clickWrap(()=>player.previousTrack()));
  bind(footerPrevTrackButton,  clickWrap(()=>player.previousTrack()));
  bind(nextTrackButton,        clickWrap(()=>player.nextTrack()));
  bind(footerNextTrackButton,  clickWrap(()=>player.nextTrack()));

  if (volumeSlider) {
    volumeSlider.addEventListener('input', () => {
      const v = Number(volumeSlider.value) / 100;
      if (player) player.setVolume(v);
    });
  }

  if (seekBar) {
    seekBar.addEventListener('input', async (e) => {
      if (!player || !nowPlaying.durationMs) return;
      const pct = Number(e.target.value) / 100;
      const pos = Math.floor(pct * nowPlaying.durationMs);
      try { await player.seek(pos); } catch {}
      nowPlaying.baseProgressMs  = pos;
      nowPlaying.baseTimestampMs = Date.now();
      renderProgressFromModel();
    });
  }

  if (toggleLeftPanelButton && leftPanel) {
    toggleLeftPanelButton.addEventListener('click', () => {
      leftPanel.classList.toggle('collapsed');
    });
  }
}

/* ===================== Web Playback SDK 初期化 ===================== */
async function initPlayer() {
  const token = await getAccessToken();

  player = new Spotify.Player({
    name: 'Tune into English Player',
    getOAuthToken: cb => cb(token),
    volume: 0.5
  });

  player.addListener('ready', async ({ device_id }) => {
    console.log('READY device_id=', device_id);
    currentDeviceId = device_id;
    try {
      const r = await fetch('/transfer_playback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id })
      });
      console.log('transfer_playback status=', r.status);
    } catch (e) { console.warn('transfer_playback failed', e); }

    await reconcileFromApi();
    await loadLyricsOnce();
    startTicker();
  });

  player.addListener('not_ready', ({ device_id }) => {
    console.warn('NOT_READY', device_id);
    if (currentDeviceId === device_id) currentDeviceId = null;
  });

  player.addListener('initialization_error', (e) => console.error('init error:', e));
  player.addListener('authentication_error', (e) => {
    console.error('auth error:', e);
    alert('Spotify認証エラー。ログインし直してください。');
    // location.href = '/logout';
  });
  player.addListener('account_error', (e) => {
    console.error('account error:', e);
    alert('Spotify Premium アカウントが必要です。');
  });

  player.addListener('player_state_changed', (state) => {
    currentPlaybackState = state || null;
    setModelFromSDK(state);
    reconcileFromApi();
  });

  const ok = await player.connect();
  console.log('player.connect() =', ok);
  if (!ok) alert('プレイヤーに接続できません。Spotifyの「Allowed Origins」にこのサイトの https URL を追加してください。');

  if (player.activateElement) {
    try { await player.activateElement(); } catch {}
  }

  bindControls();
  setInterval(reconcileFromApi, 8000);
  setInterval(pollTrackChange, 8000);
}

// SDKロード後に起動ボタンで初期化（モバイル対策）
window.onSpotifyWebPlaybackSDKReady = () => {
  if (startBtn) {
    startBtn.addEventListener('click', async () => {
      startBtn.disabled = true;
      startBtn.textContent = '起動中…';
      try {
        await initPlayer();
        startBtn.textContent = '起動完了！Spotifyのデバイス一覧を確認';
      } catch (e) {
        console.error(e);
        startBtn.textContent = '起動失敗…もう一度';
        startBtn.disabled = false;
      }
    });
  } else {
    // ボタンが無い場合は自動起動（PC向け）
    initPlayer();
  }
};

/* ===================== 初期起動（翻訳UIセット） ===================== */
document.addEventListener('DOMContentLoaded', () => {
  const saved = localStorage.getItem('translateEnabled');
  translateEnabled = (saved === null) ? true : (saved === 'true');

  if (translateToggle) {
    translateToggle.checked = translateEnabled;
    translateToggle.addEventListener('change', async () => {
      translateEnabled = !!translateToggle.checked;
      localStorage.setItem('translateEnabled', String(translateEnabled));
      applyTranslateVisibility();
      if (translateEnabled) {
        const needFetch = $content && $content.querySelector('.lyric-trans') &&
          Array.from($content.getElementsByClassName('lyric-trans')).every(el => !el.textContent);
        if (needFetch) await translateParsedLyrics();
      }
    });
  }
  applyTranslateVisibility();
});
