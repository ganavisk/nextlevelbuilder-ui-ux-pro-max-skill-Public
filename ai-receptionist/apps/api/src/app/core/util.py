"""Small, generic helpers with no natural module of their own."""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Shared list-endpoint envelope: `{items, total}`. `total` is the full
    count regardless of `limit`/`offset`, so the dashboard can render
    "1-20 of 143" and page controls without a second request."""

    items: list[T]
    total: int


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into `base`, recursing into nested dicts so a partial
    update only touches the keys it names.

    A single-level merge is not enough for a JSONB settings blob nested more
    than one key deep — e.g. tenants.settings.crm.stage_by_outcome: patching
    `{"settings": {"crm": {"enabled": False}}}` against a shallow merge
    replaces the entire `crm` object, silently deleting `stage_by_outcome`,
    `contact_properties`, everything else it held. Only dicts recurse;
    everything else (including lists) is replaced wholesale at that key —
    a caller sending `"deal_on_outcomes": []` means "replace with empty",
    and merging lists has no single unsurprising interpretation.
    """
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
