"""Supplier opt-in, end to end through the config and pipeline.

The failure mode this guards against is a newly created supplier silently
receiving automated purchase orders. Opt-in has to actually mean opt-in.
"""

from __future__ import annotations

import pytest

from cin7_reorder import schema
from cin7_reorder.config import Config, SupplierConfig


def test_lookup_key_prefers_the_slot():
    config = SupplierConfig(
        attribute_name="Auto Reorder", attribute_field="AdditionalAttribute4"
    )
    assert config.lookup_key == "AdditionalAttribute4"


def test_lookup_key_falls_back_to_the_label():
    assert SupplierConfig(attribute_name="Auto Reorder").lookup_key == "Auto Reorder"


def test_config_loads_the_slot():
    config = Config.from_dict(
        {"suppliers": {"attribute_field": "AdditionalAttribute2"}}
    )
    assert config.suppliers.lookup_key == "AdditionalAttribute2"


def test_blank_slot_in_config_falls_back_to_the_label():
    config = Config.from_dict({"suppliers": {"attribute_field": ""}})
    assert config.suppliers.attribute_field is None
    assert config.suppliers.lookup_key == "Auto Reorder"


# ---------------------------------------------------------------------------
# Truthiness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["Yes", "yes", "TRUE", "y", "1", "on", "enabled"])
def test_recognised_opt_in_values(value):
    assert SupplierConfig().is_opted_in(value) is True


@pytest.mark.parametrize("value", [None, "", "no", "false", "0", "maybe", "  "])
def test_everything_else_is_not_opted_in(value):
    """Anything unrecognised must read as off.

    Defaulting an unknown value to on would automate suppliers nobody chose.
    """
    assert SupplierConfig().is_opted_in(value) is False


def test_a_supplier_with_an_empty_slot_is_not_opted_in():
    """The whole chain: empty slot -> None -> not opted in."""
    supplier = {"ID": "s1", "Name": "Acme", "AdditionalAttribute1": ""}
    config = SupplierConfig(attribute_field="AdditionalAttribute1")

    value = schema.extract_supplier_attribute(supplier, config.lookup_key)
    assert config.is_opted_in(value) is False


def test_pin_matches_an_exact_id():
    from cin7_reorder.pipeline import _matches_pin

    assert _matches_pin("guid-123", "Acme Ltd", {"guid-123"}) is True


def test_pin_matches_a_name_fragment_case_insensitively():
    """Supplier IDs are GUIDs nobody types from memory.

    The pin exists to make "just this one supplier" easy to express safely,
    so a name fragment has to work.
    """
    from cin7_reorder.pipeline import _matches_pin

    assert _matches_pin("guid-123", "ABL Distribution Pty Ltd", {"abl"}) is True
    assert _matches_pin("guid-123", "ABL Distribution Pty Ltd", {"Distribution"}) is True


def test_pin_does_not_match_an_unrelated_supplier():
    from cin7_reorder.pipeline import _matches_pin

    assert _matches_pin("guid-123", "Acai Supply", {"abl"}) is False


def test_blank_pin_entries_match_nothing():
    """A stray empty string must not silently select every supplier."""
    from cin7_reorder.pipeline import _matches_pin

    assert _matches_pin("guid-123", "Acme Ltd", {"", "   "}) is False


def test_a_supplier_with_yes_in_the_slot_is_opted_in():
    supplier = {"ID": "s1", "Name": "Acme", "AdditionalAttribute1": "Yes"}
    config = SupplierConfig(attribute_field="AdditionalAttribute1")

    value = schema.extract_supplier_attribute(supplier, config.lookup_key)
    assert config.is_opted_in(value) is True
