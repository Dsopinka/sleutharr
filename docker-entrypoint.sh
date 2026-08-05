#!/bin/sh
# Unraid-style PUID/PGID/TZ handling, then migrate and hand off to gunicorn.
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

if [ -n "$TZ" ] && [ -f "/usr/share/zoneinfo/$TZ" ]; then
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
    echo "$TZ" > /etc/timezone
fi

# Remap the baked-in user to whatever the host wants, so files written to /config are
# owned by the user who owns the volume.
if [ "$PGID" != "$(id -g sleutharr)" ]; then
    groupmod -o -g "$PGID" sleutharr
fi
if [ "$PUID" != "$(id -u sleutharr)" ]; then
    usermod -o -u "$PUID" sleutharr
fi

mkdir -p "$SLEUTHARR_CONFIG_DIR"
chown -R sleutharr:sleutharr "$SLEUTHARR_CONFIG_DIR"

echo "Sleutharr starting as uid=$PUID gid=$PGID tz=${TZ:-UTC}"

# Migrations run before the app, as the unprivileged user so the DB file lands with the
# right ownership. SLEUTHARR_SCHEDULER=0 keeps the poller out of the migrate process.
SLEUTHARR_SCHEDULER=0 gosu sleutharr python manage.py migrate --noinput

exec gosu sleutharr "$@"
