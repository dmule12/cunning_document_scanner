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


def test_a_supplier_with_yes_in_the_slot_is_opted_in():
    supplier = {"ID": "s1", "Name": "Acme", "AdditionalAttribute1": "Yes"}
    config = SupplierConfig(attribute_field="AdditionalAttribute1")

    value = schema.extract_supplier_attribute(supplier, config.lookup_key)
    assert config.is_opted_in(value) is True
