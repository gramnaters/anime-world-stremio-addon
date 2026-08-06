"""
Gunicorn configuration file with gevent workers
Optimized for low-memory environments (Render Free: 512MB)
"""
import os

from gevent import monkey
monkey.patch_all()

# Server socket
bind = f"0.0.0.0:{os.getenv('PORT', '5000')}"  # Render uses PORT env variable
backlog = 2048

# Worker processes
# 2 workers is the sweet spot for Render free tier (512MB RAM) - keeps
# memory ~150MB peak while allowing concurrent requests.
workers = int(os.getenv('GUNICORN_WORKERS', '2'))
worker_class = 'gevent'
worker_connections = 1000
max_requests = 5000
max_requests_jitter = 500
# 120s timeout - generous enough for slow TMDB API calls + cold starts
# but short enough to not hang forever on a dead upstream.
timeout = 120
keepalive = 2

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = 'anime-world-addon'

# Server mechanics
daemon = False
pidfile = None
umask = 0
user = None
group = None
tmp_upload_dir = None

# SSL (if needed)
# keyfile = None
# certfile = None
