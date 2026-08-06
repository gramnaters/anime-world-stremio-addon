import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    """
    Configuration class
    """
    FLASK_HOST = os.getenv('FLASK_RUN_HOST', "localhost")
    FLASK_PORT = os.getenv('FLASK_RUN_PORT', "5000")
    CACHE_TYPE = 'SimpleCache'
    CACHE_DEFAULT_TIMEOUT = 600

    DEBUG = os.getenv('FLASK_DEBUG', 'False')
    
    # TMDB API Key
    TMDB_API_KEY = os.getenv('TMDB_API_KEY', '')
    
    # MediaFlow Proxy (for bypassing geo/IP blocks on scraping requests)
    SCRAPER_PROXY_URL = os.getenv('SCRAPER_PROXY_URL', '')
    SCRAPER_PROXY_PASSWORD = os.getenv('SCRAPER_PROXY_PASSWORD', '')

    # Stream delivery mode:
    #   "direct"  (default, recommended) -> return the upstream play.zephyrflick.top
    #                                       URL directly with behaviorHints.proxyHeaders
    #                                       so Stremio desktop / mobile / web+streaming-server
    #                                       fetch the video FROM the upstream themselves.
    #                                       Addon sees ZERO video bandwidth.
    #   "proxy"   (legacy)               -> route HLS through /cdn/hls/... on this server.
    #                                       Required only for Stremio Web users who do NOT
    #                                       have the Stremio Streaming Server running locally.
    #   "auto"                          -> detect per-request: desktop/mobile UA -> direct,
    #                                       browser UA -> proxy. Best of both worlds.
    STREAM_MODE = os.getenv('STREAM_MODE', 'direct').lower()

    # Whether the /cdn/hls and /subtitles proxy routes should still be registered
    # (kept mounted for backward compat; set to "0" to fully disable them).
    ENABLE_PROXY_ROUTES = os.getenv('ENABLE_PROXY_ROUTES', '1') in ('1', 'true', 'True', 'yes')
    
    # Database configuration
    DB_TYPE = os.getenv('DB_TYPE', 'sqlite')  # 'sqlite' or 'postgresql'
    DB_PATH = os.getenv('DB_PATH', 'mappings.db')  # For SQLite
    DB_CONNECTION_STRING = os.getenv('DATABASE_URL', '')  # For PostgreSQL

    # Env dependent configs
    if DEBUG in ['1', 'True', 'true']:
        PROTOCOL = "http"
        REDIRECT_URL = f"{FLASK_HOST}:{FLASK_PORT}"
    else:
        PROTOCOL = "https"
        REDIRECT_URL = f"{FLASK_HOST}"
