from __future__ import annotations

import logging
import os

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self) -> None:
        """Start the in-process poller.

        Guarded so it never starts under management commands (migrate, test, probe),
        and so the autoreloader's parent process does not start a second scheduler.
        """
        if not getattr(settings, "SCHEDULER_ENABLED", False):
            return
        if os.environ.get("RUN_MAIN") == "false":
            return

        import sys

        argv = " ".join(sys.argv)
        if any(cmd in argv for cmd in ("migrate", "makemigrations", "test", "collectstatic", "probe_services", "shell")):
            return

        from core.scheduler import start_scheduler

        try:
            start_scheduler()
        except Exception:  # noqa: BLE001 - never block startup on the poller
            logger.exception("Scheduler failed to start; the web UI will still serve.")
