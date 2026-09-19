"""
Adapter registry.

Adding a source is a matter of subclassing `JobSourceAdapter` and calling
`register()` — nothing else in the platform changes. That is what makes
"additional sources through adapters" (section 4) and the career-page system
(section 18) the same mechanism rather than two.
"""

from __future__ import annotations

from typing import Any

from .base import JobSourceAdapter

_REGISTRY: dict[str, type[JobSourceAdapter]] = {}


def register(adapter_cls: type[JobSourceAdapter]) -> type[JobSourceAdapter]:
    """Class decorator. Registers by the adapter's `slug`."""
    slug = adapter_cls.slug
    if not slug:
        raise ValueError(f"{adapter_cls.__name__} must define a slug")
    if slug in _REGISTRY and _REGISTRY[slug] is not adapter_cls:
        raise ValueError(f"Duplicate source slug {slug!r}: {_REGISTRY[slug].__name__}")
    _REGISTRY[slug] = adapter_cls
    return adapter_cls


def get_adapter(slug: str, config: dict[str, Any] | None = None) -> JobSourceAdapter:
    """Instantiate a registered adapter."""
    try:
        cls = _REGISTRY[slug]
    except KeyError:
        raise KeyError(
            f"Unknown job source {slug!r}. Registered: {', '.join(sorted(_REGISTRY))}"
        ) from None
    return cls(config)


def available_sources() -> list[str]:
    return sorted(_REGISTRY)


def is_registered(slug: str) -> bool:
    return slug in _REGISTRY
