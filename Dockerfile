FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SLEUTHARR_CONFIG_DIR=/config

# gosu drops privileges to PUID/PGID in the entrypoint; tini reaps zombies so the
# APScheduler threads shut down cleanly on container stop.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu tini curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Fixed uid/gid; the entrypoint remaps them to PUID/PGID at runtime.
RUN groupadd -g 1000 sleutharr \
    && useradd -u 1000 -g 1000 -d /app -s /bin/false sleutharr \
    && chmod +x /app/docker-entrypoint.sh

VOLUME ["/config"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8080/health/ || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
CMD ["gunicorn", "sleutharr.wsgi:application", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
