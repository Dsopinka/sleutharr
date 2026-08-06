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

# start-period is generous on purpose: first boot runs migrations, and an Unraid array
# on spinning disks under load is a lot slower than a developer laptop. Failures inside
# the start period do not count against retries, so erring long costs nothing.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
# One worker, deliberately: the APScheduler poller lives in-process, and a second worker
# would mean a second scheduler polling everything twice.
# --no-control-socket because gunicorn otherwise tries to create one under $HOME (/app,
# owned by root) and logs a permission error on every start that it never recovers from
# and never needed.
CMD ["gunicorn", "sleutharr.wsgi:application", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "120", \
     "--no-control-socket", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
