import logging
import os

from flask import Flask, render_template, url_for, redirect, make_response, request
from flask_compress import Compress
from app.routes.catalog import catalog_bp
from app.routes.manifest import manifest_blueprint
from app.routes.meta import meta_bp
from app.routes.stream import stream_bp
from app.routes.utils import cache
from config import Config

app = Flask(__name__, template_folder='./templates', static_folder='./static')
app.config.from_object('config.Config')
app.register_blueprint(manifest_blueprint)
app.register_blueprint(catalog_bp)
app.register_blueprint(meta_bp)
app.register_blueprint(stream_bp)

# The /m3u8/<id> proxy route is ALWAYS mounted because the smart-hybrid
# delivery mode uses it to proxy the master + variant + audio playlists
# (small text files, ~5 KB each, ~3 hits per play). Segment URLs are left
# absolute so Stremio fetches them directly from the CDN - zero video
# bandwidth on the addon. This is the only way to stay under Cloudflare's
# rate limit on play.zephyrix.top.
# The legacy /cdn/hls/<path> route is also mounted for STREAM_MODE=proxy users.
from app.routes.proxy import proxy_bp
app.register_blueprint(proxy_bp)
print(f"[boot] HLS proxy routes mounted (STREAM_MODE={Config.STREAM_MODE}).")
print(f"[boot] Smart-hybrid: playlists proxied, segments direct from CDN.")

# Warn loudly if TMDB_API_KEY is missing - without it, catalog/search routes
# return empty results in Stremio and users think the addon is broken.
if not Config.TMDB_API_KEY:
    print()
    print("=" * 72)
    print("  ⚠  WARNING: TMDB_API_KEY is not set!")
    print("  ⚠  Without a TMDB API key, catalog and search routes will return")
    print("  ⚠  EMPTY results in Stremio - no anime will appear.")
    print("  ⚠")
    print("  ⚠  Get a FREE key at: https://www.themoviedb.org/settings/api")
    print("  ⚠  Then set TMDB_API_KEY=your_key in your .env file and restart.")
    print("=" * 72)
    print()

Compress(app)
cache.init_app(app)

logging.basicConfig(format='%(asctime)s %(message)s')


@app.route('/')
@app.route('/configure')
@app.route('/<lang>/configure')
def index(lang=None):
    """
    Render the index page
    """
    from app.routes.manifest import MANIFEST
    import hashlib

    # Detect the addon's own public URL from the incoming request.
    # This works on Render / Heroku / Vercel / local / any host without
    # needing FLASK_RUN_HOST to be set correctly.
    #
    # Priority:
    #   1. FORCE_BASE_URL env var (explicit override, e.g. behind a custom domain)
    #   2. X-Forwarded-Host header (set by Render/Heroku reverse proxy)
    #   3. request.host (Flask's default - includes port if non-standard)
    #
    # Protocol: trust X-Forwarded-Proto if present (Render sets this to "https"),
    # otherwise fall back to request.scheme.
    force_base = os.environ.get('FORCE_BASE_URL', '').strip()
    if force_base:
        addon_base = force_base.rstrip('/')
    else:
        forwarded_host = request.headers.get('X-Forwarded-Host') or request.host
        forwarded_proto = request.headers.get('X-Forwarded-Proto') or request.scheme
        addon_base = f'{forwarded_proto}://{forwarded_host}'

    if lang:
        manifest_url = f'{addon_base}/{lang}/manifest.json'
        manifest_magnet = f'stremio://{addon_base.split("://", 1)[1]}/{lang}/manifest.json'
    else:
        manifest_url = f'{addon_base}/manifest.json'
        manifest_magnet = f'stremio://{addon_base.split("://", 1)[1]}/manifest.json'

    html = render_template('index.html',
                          manifest_url=manifest_url,
                          manifest_magnet=manifest_magnet,
                          version=MANIFEST['version'],
                          lang=lang)

    response = make_response(html)

    # Generate ETag based on version for 304 support
    etag = hashlib.md5(MANIFEST['version'].encode()).hexdigest()
    response.set_etag(etag)
    response.cache_control.max_age = 3600  # 1 hour
    response.cache_control.public = True

    # Check if client sent If-None-Match header
    if request.headers.get('If-None-Match') == etag:
        return make_response('', 304)

    return response


@app.route('/favicon.ico')
def favicon():
    """
    Render the favicon for the app
    """
    return app.send_static_file('favicon.ico')


@app.route('/callback')
def callback():
    """
    Callback URL from MyAnimeList
    :return: A webpage response with the manifest URL and Magnet URL
    """
    return redirect(url_for('index'))


if __name__ == '__main__':
    # For development only - use gunicorn in production
    app.run(host='0.0.0.0', port=5000, debug=False)
