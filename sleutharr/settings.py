"""Django settings for Sleutharr.

Everything that a user would plausibly want to change at runtime -- service URLs, API
keys, poll intervals, path mappings -- lives in the database and is edited from the
settings page. Only deployment-shaped concerns are env vars.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# /config is the Unraid/docker convention: the single writable volume holding the DB,
# the secret key, and logs. Falls back to ./config so a git clone runs without root.
CONFIG_DIR = Path(os.environ.get("SLEUTHARR_CONFIG_DIR", "/config"))
try:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not os.access(CONFIG_DIR, os.W_OK):
        raise OSError
except OSError:
    CONFIG_DIR = BASE_DIR / "config"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _secret_key() -> str:
    """Persist a generated key so sessions survive container restarts."""
    env = os.environ.get("SLEUTHARR_SECRET_KEY")
    if env:
        return env
    key_file = CONFIG_DIR / "secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_urlsafe(50)
    key_file.write_text(key)
    key_file.chmod(0o600)
    return key


SECRET_KEY = _secret_key()
DEBUG = os.environ.get("SLEUTHARR_DEBUG", "").lower() in {"1", "true", "yes"}

# This is a LAN-local tool that sits behind whatever reverse proxy the user already runs;
# host validation is not the security boundary here and a wrong default just breaks it.
ALLOWED_HOSTS = ["*"]
CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get("SLEUTHARR_CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "core",
]

MIDDLEWARE = [
    "django.middleware.gzip.GZipMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "sleutharr.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "core" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "core.context.app_context",
            ],
        },
    },
]

WSGI_APPLICATION = "sleutharr.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": CONFIG_DIR / "sleutharr.db",
        "OPTIONS": {
            # WAL lets the APScheduler poll threads write while a page renders.
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 30,
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TZ", "UTC")
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = CONFIG_DIR / "static"
STATICFILES_DIRS = [BASE_DIR / "core" / "static"]

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "UNAUTHENTICATED_USER": None,
}

# Set false to run the web process without the poller (e.g. a second replica, or tests).
SCHEDULER_ENABLED = os.environ.get("SLEUTHARR_SCHEDULER", "1").lower() not in {
    "0",
    "false",
    "no",
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)-7s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # Chatty and never interesting for this workload.
        "apscheduler.executors.default": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
    },
}
