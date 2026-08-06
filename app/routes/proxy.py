import os
import re
import requests
from flask import Blueprint, Response, request, abort
from urllib.parse import unquote, urljoin
from cachetools import TTLCache
from config import Config
from .utils import get_random_agent

proxy_bp = Blueprint('proxy', __name__)

# Store subtitle mappings with TTL (1 hour expiration, max 500 entries)
subtitle_mappings = TTLCache(maxsize=500, ttl=3600)

# Store m3u8 playlist mappings with TTL (10 min expiration, max 1000 entries)
# Key: random id, Value: dict with 'url', 'headers' (Referer, UA, etc.)
import secrets
playlist_mappings = TTLCache(maxsize=1000, ttl=600)

# The upstream player domain - kept in sync with app.players.zephyrflick
UPSTREAM_BASE = os.getenv("ZEPHYR_PLAYER_BASE", "https://play.zephyrix.top").rstrip("/")

# Cookie jar shared across all requests to the upstream - lets us benefit from
# any Cloudflare clearance cookies we manage to obtain.
_cookie_jar = requests.cookies.RequestsCookieJar()

def _upstream_headers(extra: dict = None) -> dict:
    """Build headers for upstream requests. Cloudflare on play.zephyrix.top
    is highly sensitive to header fingerprints - keep headers MINIMAL.
    Empirically: just User-Agent + Referer works most reliably. Adding
    Accept / Accept-Encoding / Sec-Fetch-* triggers 403."""
    h = {
        'User-Agent': get_random_agent(),
        'Referer': f'{UPSTREAM_BASE}/',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    if extra:
        h.update(extra)
    return h

def _fetch_upstream(url: str, max_retries: int = 3) -> requests.Response:
    """Fetch a URL from upstream with retry on 403 (Cloudflare rate-limits).
    Uses a shared cookie jar so any cf_clearance cookie we obtain persists."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=_upstream_headers(), timeout=20,
                             cookies=_cookie_jar)
            # Capture any Set-Cookie from the response
            if r.cookies:
                for c in r.cookies:
                    _cookie_jar.set_cookie(c)
            if r.status_code == 200:
                return r
            if r.status_code == 403:
                # Cloudflare rate-limit / challenge - back off and retry
                import time
                time.sleep(1.0 * (attempt + 1))
                continue
            # Other status codes - return as-is
            return r
        except Exception as e:
            last_exc = e
            import time
            time.sleep(0.5 * (attempt + 1))
    if last_exc:
        raise last_exc
    return r  # type: ignore

def reorder_audio_tracks(m3u8_content: str, preferred_lang: str) -> str:
    """
    Reorder audio tracks in m3u8 to set preferred language as DEFAULT=YES and first
    """
    lines = m3u8_content.split('\n')
    audio_tracks = []
    other_lines = []
    preferred_track = None

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('#EXT-X-MEDIA:TYPE=AUDIO'):
            # Extract language from track
            lang_match = re.search(r'LANGUAGE="([^"]+)"', line)
            if lang_match:
                track_lang = lang_match.group(1)
                if track_lang == preferred_lang:
                    # Set as DEFAULT=YES
                    line = re.sub(r'DEFAULT=(YES|NO)', 'DEFAULT=YES', line)
                    preferred_track = line
                else:
                    # Set as DEFAULT=NO
                    line = re.sub(r'DEFAULT=(YES|NO)', 'DEFAULT=NO', line)
                    audio_tracks.append(line)
            else:
                audio_tracks.append(line)
        else:
            other_lines.append((i, line))
        i += 1

    # Rebuild m3u8 with preferred track first
    result = []
    audio_inserted = False

    for idx, line in other_lines:
        result.append(line)
        # Insert audio tracks after #EXT-X-VERSION line
        if line.startswith('#EXT-X-VERSION') and not audio_inserted:
            if preferred_track:
                result.append(preferred_track)
            result.extend(audio_tracks)
            audio_inserted = True

    return '\n'.join(result)

def _rewrite_m3u8_urls(content: str, playlist_id: str) -> str:
    """Rewrite all URLs in an m3u8 playlist to point back at our /m3u8/ proxy
    IF they point at the upstream (zephyrix). Segment URLs on the CDN
    (s7.as-cdn*.top) are left ABSOLUTE so Stremio fetches them directly -
    those CDNs don't rate-limit and Stremio's parallel segment fetches
    won't hammer the upstream.

    This is the "smart hybrid": proxy only the small text playlists
    (a few KB each, only 2-3 hits per play attempt -> stays under
    Cloudflare's rate limit), let Stremio fetch the large video segments
    directly from the CDN (zero video bandwidth on addon)."""
    # Rewrite URI="..." inside EXT-X-MEDIA tags (audio track playlists)
    def _rewrite_uri(m):
        u = m.group(1)
        if u.startswith('/'):
            u = f'{UPSTREAM_BASE}{u}'
        if UPSTREAM_BASE in u:
            # This is a zephyrix playlist URL - proxy it
            sub_id = 'pl_' + secrets.token_hex(8)
            playlist_mappings[sub_id] = {'url': u}
            return f'URI="{request.host_url.rstrip("/")}/m3u8/{sub_id}"'
        return m.group(0)

    content = re.sub(r'URI="([^"]+)"', _rewrite_uri, content)

    # Rewrite standalone URL lines (variant playlists)
    lines = content.split('\n')
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            out.append(line)
            continue
        # This is a URL line - either a variant playlist (on zephyrix) or a segment
        u = stripped
        if u.startswith('/'):
            u = f'{UPSTREAM_BASE}{u}'
        if UPSTREAM_BASE in u:
            # Variant playlist on zephyrix - proxy it
            sub_id = 'pl_' + secrets.token_hex(8)
            playlist_mappings[sub_id] = {'url': u}
            out.append(f'{request.host_url.rstrip("/")}/m3u8/{sub_id}')
        else:
            # Segment on CDN (s7.as-cdn*.top) - leave absolute
            out.append(line)
    return '\n'.join(out)


@proxy_bp.route('/m3u8/<playlist_id>')
def proxy_m3u8(playlist_id):
    """Proxy an m3u8 playlist from upstream. Rewrites internal URLs to point
    back at this proxy (for sub-playlists) or leave them absolute (for CDN
    segments). This keeps total zephyrix hits to ~3 per play attempt, well
    under Cloudflare's rate limit."""
    mapping = playlist_mappings.get(playlist_id)
    if not mapping:
        abort(404, description="Playlist mapping expired or not found")

    upstream_url = mapping['url']

    try:
        r = _fetch_upstream(upstream_url)
        if r.status_code != 200:
            abort(502, description=f"Upstream returned {r.status_code}")

        content = r.text
        # Re-write URLs in the m3u8 to use our proxy for sub-playlists
        content = _rewrite_m3u8_urls(content, playlist_id)

        response = Response(content, mimetype='application/vnd.apple.mpegurl')
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = '*'
        response.headers['Cache-Control'] = 'no-cache'
        return response
    except Exception as e:
        print(f"Error proxying m3u8 {playlist_id}: {e}")
        abort(502)


@proxy_bp.route('/cdn/hls/<path:path>')
@proxy_bp.route('/<lang>/cdn/hls/<path:path>')
def proxy_hls(path, lang=None):
    """
    Legacy HLS proxy: fetches master.m3u8 from upstream, optionally reorders
    audio tracks, rewrites internal URLs to point at the upstream directly.
    Kept for STREAM_MODE=proxy users who want ALL traffic (including segments)
    to go through the addon.
    """
    # Get original URL with query params
    query_string = request.query_string.decode('utf-8')
    original_url = f"{UPSTREAM_BASE}/cdn/hls/{path}"
    if query_string:
        original_url += f"?{query_string}"

    try:
        r = _fetch_upstream(original_url)
        if r.status_code != 200:
            abort(502)

        content = r.text

        # Reorder audio tracks if language specified
        if lang:
            content = reorder_audio_tracks(content, lang)

        # Rewrite relative URLs to absolute URLs pointing to original server
        content = re.sub(
            r'URI="(/hls/[^"]+)"',
            f'URI="{UPSTREAM_BASE}\\1"',
            content
        )
        content = re.sub(
            r'^(/hls/.+)$',
            f'{UPSTREAM_BASE}\\1',
            content,
            flags=re.MULTILINE
        )

        response = Response(content, mimetype='application/vnd.apple.mpegurl')
        # Add CORS headers for Stremio Web
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = '*'
        return response
    except Exception as e:
        print(f"Error proxying HLS: {e}")
        abort(502)

@proxy_bp.route('/subtitles/<subtitle_id>')
def proxy_subtitle(subtitle_id):
    """
    Proxy subtitle files with correct content-type
    """
    original_url = subtitle_mappings.get(subtitle_id)
    if not original_url:
        abort(404)

    try:
        r = _fetch_upstream(original_url)
        if r.status_code != 200:
            abort(502)

        # Determine content type based on file extension
        if subtitle_id.endswith('.srt'):
            content_type = 'application/x-subrip'
        else:
            content_type = 'text/vtt'

        response = Response(r.content, mimetype=content_type)
        # Add CORS headers
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = '*'
        return response
    except Exception as e:
        print(f"Error proxying subtitle: {e}")
        abort(502)
