"""Template helpers."""

from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def diagnosis_title(code: str) -> str:
    """Plain-English label for a diagnosis code."""
    from core.rules import title_for

    return title_for(code or "")
