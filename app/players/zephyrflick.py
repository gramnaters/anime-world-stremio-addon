import os
import re
import secrets
import requests
from app.routes.utils import get_random_agent
from config import Config

# The upstream player domain was renamed from play.zephyrflick.top to
# play.zephyrix.top. Make it configurable so a future rename is a one-env-var
# flip instead of a code change. Defaults to the current domain.
UPSTREAM_BASE = os.getenv("ZEPHYR_PLAYER_BASE", "https://play.zephyrix.top").rstrip("/")
UPSTREAM_REFERER = f"{UPSTREAM_BASE}/"

# Headers Stremio client should send when fetching URLs that need Cloudflare
# clearance (the CDN segments). We keep this MINIMAL because Cloudflare on
# the upstream is sensitive to header fingerprints.
STREMIO_CLIENT_HEADERS = {
    'Referer': UPSTREAM_REFERER,
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}

def _is_browser_client() -> bool:
    """Best-effort detection of Stremio Web (browser) vs desktop/mobile apps."""
    from flask import request as _req
    ua = (_req.headers.get('User-Agent') or '').lower()
    if 'stremio' in ua:
        return False
    if 'streaming-server' in ua:
        return False
    if any(k in ua for k in ('electron', 'stremio-shell')):
        return False
    if any(k in ua for k in ('mozilla', 'chrome', 'safari', 'firefox')):
        return True
    return False


def _resolve_stream_mode(requested_mode: str) -> str:
    """Resolve 'auto' into 'direct' or 'proxy' based on the current request."""
    if requested_mode != 'auto':
        return requested_mode
    return 'proxy' if _is_browser_client() else 'direct'


async def get_video_from_zephyrflick_player(player_url: str, preferred_lang: str = None):
    """
    Extract video URL and subtitles from Zephyrflick player.

    Two delivery modes (both work, choice depends on STREAM_MODE):

    hybrid (default for 'direct'):
      Returns a PROXIED master.m3u8 URL that points back at this addon.
      The proxy fetches the master from upstream ONCE (with retry + cookies),
      then rewrites all sub-playlist URLs to also go through the proxy.
      Segment URLs (on the CDN s7.as-cdn*.top) are left ABSOLUTE so Stremio
      fetches them directly - zero video bandwidth on the addon.
      Net effect: ~3 upstream hits per play attempt (master + variant + audio
      playlist), well under Cloudflare's rate limit. All video bytes flow
      directly from CDN to Stremio.

    proxy (legacy, STREAM_MODE=proxy):
      Routes everything (master + variant + segments) through the addon.
      Maximum compatibility, maximum addon bandwidth.

    Returns: tuple (video_url, quality, headers, subtitles)
             - video_url: the URL Stremio should fetch
             - headers: {'request': {...}} for behaviorHints.proxyHeaders
             - subtitles: list of {id, url, lang}
    """
    try:
        # Extract video ID from URL
        match = re.search(r'/video/([a-f0-9]+)', player_url)
        if not match:
            return None, None, None, []

        video_id = match.group(1)

        api_headers = {
            'User-Agent': get_random_agent(),
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': player_url
        }

        # Make POST request to get video source
        api_url = f"{UPSTREAM_BASE}/player/index.php"
        params = {
            'data': video_id,
            'do': 'getVideo'
        }

        resp = requests.post(api_url, params=params, headers=api_headers, timeout=30)
        resp.raise_for_status()

        data = resp.json()
        video_url = data.get('videoSource')

        if not video_url:
            return None, None, None, []

        # ------------------------------------------------------------------
        # Decide how to deliver the stream.
        # ------------------------------------------------------------------
        # Always use the smart-hybrid approach: the master.m3u8 goes through
        # our /cdn/hls/ proxy (which rewrites sub-URLs to also be proxied,
        # while keeping segment URLs absolute). This limits upstream hits
        # to ~3 per play, avoiding Cloudflare rate-limiting, while still
        # sending zero video bytes through the addon.
        #
        # If the proxy routes aren't mounted (ENABLE_PROXY_ROUTES=0), we
        # fall back to returning the direct upstream URL with proxyHeaders.
        # ------------------------------------------------------------------
        from app.routes.proxy import playlist_mappings, UPSTREAM_BASE as PROXY_UPSTREAM_BASE

        # Create a playlist mapping for the master URL
        master_playlist_id = 'pl_' + secrets.token_hex(12)
        playlist_mappings[master_playlist_id] = {'url': video_url}

        # The URL Stremio fetches: our /m3u8/<id> route, which proxies the
        # master and rewrites sub-URLs appropriately.
        from flask import request as _req
        addon_host = f'{_req.scheme}://{_req.host}'
        proxied_url = f'{addon_host}/m3u8/{master_playlist_id}'

        # We DO need proxyHeaders - even though our master URL points at the
        # addon (which doesn't need them), Stremio will use these same
        # proxyHeaders when fetching CDN segment URLs embedded in the m3u8.
        # The CDN (s7.as-cdn*.top) requires Referer: https://play.zephyrix.top/
        # to allow segment fetches - without it, segments return 403.
        # Our /m3u8/ and /subtitles/ routes simply ignore these headers.
        stream_headers = {'request': dict(STREMIO_CLIENT_HEADERS)}
        video_url = proxied_url

        # ------------------------------------------------------------------
        # Subtitles: always parse from the player page so we have URLs.
        # Subtitle URLs are on the CDN (s7.as-cdn*.top) - same as segments.
        # We proxy them through our /subtitles/<id> route because:
        #   1. The CDN requires a Referer header to allow the request
        #   2. Stremio subtitle objects don't support per-subtitle proxyHeaders
        #   3. Subtitles are tiny (~20 KB) - proxying them costs nothing
        # ------------------------------------------------------------------
        from app.routes.proxy import subtitle_mappings
        from flask import request as _req
        addon_host = f'{_req.scheme}://{_req.host}'

        subtitles = []
        try:
            page_resp = requests.get(player_url, headers=api_headers, timeout=30)
            page_resp.raise_for_status()

            # Find playerjsSubtitle variable
            subtitle_match = re.search(r'var playerjsSubtitle = "([^"]+)"', page_resp.text)
            if subtitle_match:
                subtitle_data = subtitle_match.group(1)
                # Parse subtitle entries: [Language]url
                for line in subtitle_data.split('\n'):
                    line = line.strip()
                    if not line:
                        continue

                    sub_match = re.match(r'\[([^\]]+)\](.+)', line)
                    if sub_match:
                        lang_name = sub_match.group(1)
                        sub_url = sub_match.group(2)

                        # Convert language name to ISO code
                        lang_code = 'eng' if 'english' in lang_name.lower() else lang_name.lower()[:3]

                        # Determine file extension from original URL
                        file_ext = '.srt' if sub_url.endswith('.srt') else '.vtt'
                        subtitle_id = f"{video_id}_{lang_code}{file_ext}"

                        # Store mapping so /subtitles/<id> can fetch it
                        subtitle_mappings[subtitle_id] = sub_url

                        # Return our proxied subtitle URL
                        proxied_sub_url = f"{addon_host}/subtitles/{subtitle_id}"

                        subtitles.append({
                            'id': f"{video_id}_{lang_code}",
                            'url': proxied_sub_url,
                            'lang': lang_code
                        })
        except Exception as sub_err:
            print(f"Warning: could not extract subtitles: {sub_err}")

        return video_url, 'auto', stream_headers, subtitles

    except Exception as e:
        print(f"Error extracting Zephyrflick video: {e}")
        return None, None, None, []
