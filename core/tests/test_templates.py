"""Template hygiene checks.

Django's `{# ... #}` comment syntax is single-line only. A comment spanning lines is not
a comment -- it renders as visible text on the page. It is silent, easy to write, and
survives every functional test, so it gets a check of its own.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

TEMPLATE_ROOT = Path(settings.BASE_DIR) / "core" / "templates"


class TemplateCommentTests(TestCase):
    def test_no_multiline_django_comments(self):
        offenders = []
        for path in TEMPLATE_ROOT.rglob("*.html"):
            text = path.read_text()
            for match in re.finditer(r"\{#", text):
                remainder = text[match.start() :]
                end = remainder.find("#}")
                if end == -1 or "\n" in remainder[:end]:
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.name}:{line}")
        self.assertEqual(
            offenders,
            [],
            "Multi-line {# #} comments render as visible page text. "
            "Use one line, or {% comment %}...{% endcomment %}.",
        )

    def test_every_template_parses(self):
        from django.template.loader import get_template

        for path in TEMPLATE_ROOT.rglob("*.html"):
            with self.subTest(template=path.name):
                get_template(f"core/{path.name}")
