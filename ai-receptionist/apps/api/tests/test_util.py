"""core.util.deep_merge — regression coverage for the bug it fixes: a
single-level merge on tenants.settings silently deleted sibling keys nested
under whichever top-level key a PATCH touched (see routes.py's docstring)."""

from app.core.util import Page, deep_merge


def test_top_level_keys_merge() -> None:
    assert deep_merge({"a": 1, "b": 2}, {"b": 3}) == {"a": 1, "b": 3}


def test_nested_dict_merges_without_deleting_siblings() -> None:
    """The exact scenario that broke: patching settings.crm.enabled must not
    erase settings.crm.stage_by_outcome."""
    base = {
        "crm": {
            "enabled": True,
            "stage_by_outcome": {"default": "new"},
            "deal_min_score": 30,
        }
    }
    patch = {"crm": {"enabled": False}}
    result = deep_merge(base, patch)
    assert result["crm"]["enabled"] is False
    assert result["crm"]["stage_by_outcome"] == {"default": "new"}
    assert result["crm"]["deal_min_score"] == 30


def test_merges_recursively_at_any_depth() -> None:
    base = {"crm": {"stage_by_outcome": {"default": "new", "booked": "won"}}}
    patch = {"crm": {"stage_by_outcome": {"booked": "closed"}}}
    result = deep_merge(base, patch)
    assert result["crm"]["stage_by_outcome"] == {"default": "new", "booked": "closed"}


def test_list_values_are_replaced_wholesale_not_merged() -> None:
    """A caller sending an empty list means 'replace with empty' — merging
    list contents has no single unsurprising interpretation, so lists never
    recurse, only dicts do."""
    base = {"crm": {"deal_on_outcomes": ["appointment_booked", "lead_qualified"]}}
    patch = {"crm": {"deal_on_outcomes": []}}
    assert deep_merge(base, patch)["crm"]["deal_on_outcomes"] == []


def test_non_dict_patch_value_overwrites_dict_base_value() -> None:
    """Type mismatch (base has a dict, patch has a scalar) replaces rather
    than erroring — the patch value always wins at the leaf it names."""
    assert deep_merge({"crm": {"a": 1}}, {"crm": "disabled"}) == {"crm": "disabled"}


def test_new_key_is_added() -> None:
    assert deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_base_is_not_mutated() -> None:
    base = {"crm": {"enabled": True}}
    deep_merge(base, {"crm": {"enabled": False}})
    assert base["crm"]["enabled"] is True


def test_empty_patch_is_a_no_op() -> None:
    base = {"a": 1, "b": {"c": 2}}
    assert deep_merge(base, {}) == base


# ── Page ─────────────────────────────────────────────────────────────────


def test_page_carries_items_and_total() -> None:
    page = Page[int](items=[1, 2, 3], total=10)
    assert page.items == [1, 2, 3]
    assert page.total == 10
