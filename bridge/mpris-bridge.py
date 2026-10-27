#!/usr/bin/env python3
"""
mpris-bridge: serves what your browser is playing (via MPRIS / playerctl) as JSON on http://127.0.0.1:5000

  /              the widget page itself (widget/now-playing.html), so OBS can load it over plain HTTP
  /now-playing   current sessions, used by widget/now-playing.html
  /debug         raw playerctl metadata + what the userscript has reported (for troubleshooting)
  POST /meta     receives title / artist / artwork from userscript/now-playing.user.js

Requires: Linux, Python 3, playerctl.
"""
import json
import subprocess
import os
import re
import sys
import base64
import threading
import time
from urllib.parse import unquote, urlparse
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ---------------------------------------------------------------- settings
# BROWSER players are only reported when the page URL is on one of these hosts. Add more sites here.
ALLOWED_URL_HOSTS = ("youtube.com", "youtu.be", "open.spotify.com", "soundcloud.com")
# Chromium-based browsers often send no xesam:url. True = keep those players, False = drop them.
ALLOW_UNKNOWN_URL = True
# Which MPRIS player names count as browsers (matched against `playerctl -l`, case-insensitive substring)
BROWSER_NAMES = ("firefox", "chrome", "chromium", "brave", "edge", "vivaldi", "opera")
# NATIVE apps are always reported, with no URL check. Same substring matching as above.
# Add more here, e.g. "vlc", "mpv", "strawberry", "rhythmbox", "elisa", "amberol"
NATIVE_PLAYERS = ("spotify", "vlc")
# Widget page served at http://127.0.0.1:5000/ . Leave empty to auto-find widget/now-playing.html
# next to this script (works when running from the repo). If you copy the bridge somewhere else
# (e.g. ~/.local/bin), set the full path to now-playing.html here, or via $NOW_PLAYING_WIDGET.
WIDGET_FILE = os.environ.get("NOW_PLAYING_WIDGET", "")

SEP = "~~SEP~~"
FORMAT = SEP.join("{{%s}}" % k for k in (
    "xesam:title", "xesam:artist", "xesam:url", "mpris:artUrl",
    "mpris:length", "status", "xesam:albumArtist", "xesam:album"))


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- helpers
def is_allowed_url(url):
    if not url:
        return ALLOW_UNKNOWN_URL
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_URL_HOSTS)


def sniff_mime(data):
    # Chromium/Brave write art to extensionless temp files, so detect the type from the bytes
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def youtube_id(url):
    if not url:
        return None
    m = re.search(r"[?&]v=([\w-]{11})", url) or re.search(r"youtu\.be/([\w-]{11})", url)
    return m.group(1) if m else None


_art_cache = {}   # raw art url -> processed art (successes only)
_art_logged = {}  # player -> last raw value we logged


def process_art_url(raw_art, page_url, player):
    """Return something an <img> can load: data: URI, https URL, or '' if nothing works."""
    art = ""
    raw_art = (raw_art or "").strip()

    if raw_art:
        if raw_art in _art_cache:
            return _art_cache[raw_art]

        path = None
        if raw_art.startswith("file://"):
            path = unquote(urlparse(raw_art).path)
        elif raw_art.startswith("/"):
            path = raw_art

        if path:
            try:
                with open(path, "rb") as f:
                    data = f.read()
                if data:
                    art = f"data:{sniff_mime(data)};base64,{base64.b64encode(data).decode()}"
            except Exception as e:
                if _art_logged.get(player) != raw_art:
                    log(f"[art] cannot read {path}: {e}")
        elif raw_art.startswith(("http://", "https://", "data:")):
            art = raw_art

        if art:
            if len(_art_cache) > 20:
                _art_cache.clear()
            _art_cache[raw_art] = art

    # Fallback: build the thumbnail from the YouTube video id
    if not art:
        vid = youtube_id(page_url)
        if vid:
            art = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
        else:
            # fallback to a transparent 1x1 spacer pixel so the widget never crashes
            art = "https://flaticon.com"

    if _art_logged.get(player) != raw_art:
        _art_logged[player] = raw_art
        shown = (raw_art[:100] + "...") if len(raw_art) > 100 else raw_art
        kind = "none" if not art else ("data" if art.startswith("data:") else art[:60])
        log(f"[art] {player}: raw={shown!r} -> {kind}")
    return art


def clean_title(title):
    # Browsers sometimes report the page title; strip the site suffix
    for suffix in (" | YouTube Music", " - YouTube Music", " - YouTube"):
        if title.endswith(suffix):
            return title[: -len(suffix)]
    return title


def clean_artist(artist):
    # YouTube auto-generated channels are named "Artist - Topic"
    return re.sub(r"\s+-\s+Topic$", "", artist.strip())


def parse_time_to_seconds(val_str):
    if not val_str:
        return 0.0
    try:
        val = float(val_str)
        if val > 1_000_000:
            val = val / 1_000_000.0
        elif val > 100_000:
            val = val / 1_000.0
        if val > 6 * 3600 or val < 0:   # was 1 h, which broke long mixes / podcasts
            return 0.0
        return val
    except ValueError:
        return 0.0


def is_native(player):
    return any(n in player.lower() for n in NATIVE_PLAYERS)


def active_players():
    r = subprocess.run(["playerctl", "-l"], capture_output=True, text=True, timeout=0.8)
    if r.returncode != 0 or not r.stdout.strip():
        return []
    players = [p.strip() for p in r.stdout.strip().split("\n") if p.strip()]
    return [p for p in players if is_native(p) or any(b in p.lower() for b in BROWSER_NAMES)]


# ---------------------------------------------------------------- page metadata
# Brave often gives MPRIS only a tab title. The userscript (now-playing.user.js) reads the real
# artist / artwork from the YouTube page and POSTs it to /meta; we merge it in here.
META_TTL = 8.0  # seconds a page report stays valid
_page_meta = {}  # normalised title -> {"data": {...}, "ts": float}  (one entry per open tab/video)


def _norm(s):
    return re.sub(r"\W+", "", (s or "").lower())


GENERIC_TITLES = ("youtube", "youtubemusic", "")


def page_meta_for(title, mpris_len=0.0, status=""):
    """Find the page report that belongs to this MPRIS session.
    1) same title  2) same length (+-2.5s)  3) if the browser only gives a generic tab title
    ("YouTube Music"), the freshest tab that is actually playing."""
    now = time.time()
    for k in [k for k, v in _page_meta.items() if now - v["ts"] > META_TTL]:
        del _page_meta[k]
    if not _page_meta:
        return None
    b = _norm(title)
    if b and b not in GENERIC_TITLES:
        if b in _page_meta:
            return _page_meta[b]["data"]
        for k, v in _page_meta.items():
            if k and (k in b or b in k):
                return v["data"]
    if mpris_len > 10:
        close = [v for v in _page_meta.values()
                 if v["data"]["duration"] > 10 and abs(v["data"]["duration"] - mpris_len) <= 2.5]
        if close:
            return max(close, key=lambda v: v["ts"])["data"]
    if b in GENERIC_TITLES:
        pool = list(_page_meta.values())
        if status == "Playing":
            pool = [v for v in pool if v["data"]["playing"]]
        if pool:
            return max(pool, key=lambda v: v["ts"])["data"]
    return None


def store_page_meta(raw):
    def s(key, limit=500):
        v = raw.get(key, "")
        return v.strip()[:limit] if isinstance(v, str) else ""
    url = s("url", 2000)
    if not url or not is_allowed_url(url) or not s("title"):
        return False
    art = s("art", 2000)
    if art and not art.startswith(("http://", "https://")):
        art = ""
    if len(_page_meta) > 20:
        _page_meta.clear()
    _page_meta[_norm(s("title"))] = {"ts": time.time(), "data": {
        "title": s("title"), "artist": s("artist"), "album": s("album"), "art": art, "url": url,
        "playing": bool(raw.get("playing")),
        "duration": float(raw["duration"]) if isinstance(raw.get("duration"), (int, float)) else 0.0}}
    _cache["ts"] = 0.0  # make the next poll pick this up immediately
    return True


# ---------------------------------------------------------------- main data
def get_playerctl_data():
    sessions = []
    try:
        for player in active_players():
            meta_res = subprocess.run(
                ["playerctl", "-p", player, "metadata", "-f", FORMAT],
                capture_output=True, text=True, timeout=0.8)
            if meta_res.returncode != 0 or not meta_res.stdout.strip():
                continue

            parts = meta_res.stdout.strip("\n").split(SEP)
            parts += [""] * (8 - len(parts))
            title, artist, url, raw_art, length_str, status, album_artist, album = [p.strip() for p in parts[:8]]
            title = clean_title(title)
            artist = artist or album_artist
            native = is_native(player)
            # the userscript only describes browser tabs, so native apps skip it
            meta = None if native else page_meta_for(title, parse_time_to_seconds(length_str), status)
            if meta:
                # a fresh report from a YouTube page: trust it for title/artist/art/url
                title = clean_title(meta["title"]) or title
                artist = meta["artist"] or artist
                album = meta["album"] or album
                raw_art = meta["art"] or raw_art
                url = url or meta["url"]
            elif not native and not is_allowed_url(url):
                continue
            artist = clean_artist(artist)

            art_url = process_art_url(raw_art, url, player)
            duration = parse_time_to_seconds(length_str)
            if meta and meta.get("duration", 0) > 10:
                duration = meta["duration"]

            raw_position = 0.0
            pos_res = subprocess.run(["playerctl", "-p", player, "position"],
                                     capture_output=True, text=True, timeout=0.5)
            if pos_res.returncode == 0:
                try:
                    val = float(pos_res.stdout.strip())
                    raw_position = val / 1_000_000.0 if val > 100_000 else val
                except ValueError:
                    pass

            if duration <= 10:
                duration = max(raw_position + 60.0, 180.0)

            position = raw_position
            if position > duration:
                duration = position + 5.0

            if title or status in ["Playing", "Paused"]:
                thumb_b64 = ""
                if art_url.startswith("data:") and ";base64," in art_url:
                    thumb_b64 = art_url.split(";base64,", 1)[1]
                status_code = {"Playing": 4, "Paused": 5, "Stopped": 3}.get(status, 0)
                end_ms = int(duration * 1000)
                sessions.append({
                    # fields used by the self-hosted widget
                    "player_name": player,
                    "playback_status": status,
                    "duration": duration,
                    "position": position,
                    "url": url,
                    # fields matching the SMTC Bridge schema (used by widgets.nutty.gg)
                    "source_app_id": player,
                    "media_properties": {
                        "Title": title,
                        "Artist": artist,
                        "AlbumTitle": album,
                        "AlbumArtist": artist,
                        "Thumbnail": thumb_b64,
                        "AlbumTrackCount": 0,
                        "TrackNumber": 0,
                        "Genres": [],
                        "Subtitle": "",
                        "art_url": art_url
                    },
                    "playback_info": {
                        "PlaybackStatus": status_code,
                        "PlaybackType": 1,
                        "PlaybackRate": 1.0,
                        "IsShuffleActive": False,
                        "AutoRepeatMode": 0
                    },
                    "timeline_properties": {
                        "Position": int(position * 1000),
                        "StartTime": 0,
                        "EndTime": end_ms,
                        "MinSeekTime": 0,
                        "MaxSeekTime": end_ms,
                        "LastUpdatedTime": datetime.now(timezone.utc).isoformat()
                    }
                })
    except Exception as e:
        log(f"[bridge] error: {e!r}")

    sessions.sort(key=lambda s: s["playback_status"] != "Playing")   # playing players first (stable)
    current = sessions[0]["source_app_id"] if sessions else ""
    return {"app_version": "1.0.0", "current_session_id": current, "sessions": sessions}


def get_debug():
    """Raw metadata straight from playerctl, so we can see exactly what the browser reports."""
    out = []
    try:
        for player in active_players():
            res = subprocess.run(["playerctl", "-p", player, "metadata"],
                                 capture_output=True, text=True, timeout=1.5)
            raw = res.stdout.strip()
            art = ""
            for line in raw.splitlines():
                if "mpris:artUrl" in line:
                    art = line.split("mpris:artUrl", 1)[1].strip()
            info = {"player": player, "raw_metadata": raw.splitlines(), "artUrl": art}
            path = unquote(urlparse(art).path) if art.startswith("file://") else (art if art.startswith("/") else None)
            if path:
                info["art_file_exists"] = os.path.exists(path)
                try:
                    with open(path, "rb") as f:
                        info["art_file_bytes"] = len(f.read())
                except Exception as e:
                    info["art_file_error"] = repr(e)
            out.append(info)
    except Exception as e:
        out.append({"error": repr(e)})
    out.append({"page_meta": [dict(v["data"], age_seconds=round(time.time() - v["ts"], 1)) for v in _page_meta.values()]})
    return out


def find_widget():
    """Locate now-playing.html: repo layout (../widget/) first, then next to this script."""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.expanduser(WIDGET_FILE) if WIDGET_FILE else "",
              os.path.join(here, "..", "widget", "now-playing.html"),
              os.path.join(here, "widget", "now-playing.html"),
              os.path.join(here, "now-playing.html")):
        if p and os.path.isfile(p):
            return p
    return None


_cache = {"data": {"sessions": []}, "ts": 0.0}
_lock = threading.Lock()


def cached_data():
    # Reuse the last result for 0.5s so many polls don't spawn many playerctl calls
    with _lock:
        if time.time() - _cache["ts"] > 0.5:
            _cache["data"] = get_playerctl_data()
            _cache["ts"] = time.time()
        return _cache["data"]


class MPRISBridgeHandler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.split('?', 1)[0] in ('/', '/widget'):
            wp = find_widget()
            if not wp:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"now-playing.html not found next to the bridge")
                return
            with open(wp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith('/now-playing') or self.path.startswith('/sessions'):
            data = cached_data()
            if self.path.startswith('/sessions'):
                data = data["sessions"]
        elif self.path.startswith('/debug'):
            data = get_debug()
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(data, indent=2 if self.path.startswith('/debug') else None).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.path.startswith('/meta'):
            self.send_response(404)
            self.end_headers()
            return
        try:
            n = min(int(self.headers.get('Content-Length', 0)), 65536)
            raw = json.loads(self.rfile.read(n) or b"{}")
            ok = isinstance(raw, dict) and store_page_meta(raw)
        except Exception:
            ok = False
        self.send_response(204 if ok else 400)
        self._cors()
        self.end_headers()

    def log_message(self, format, *args):
        if "favicon.ico" in format % args:
            return
        super().log_message(format, *args)


if __name__ == '__main__':
    server_address = ('127.0.0.1', 5000)
    httpd = ThreadingHTTPServer(server_address, MPRISBridgeHandler)
    httpd.serve_forever()
