"""The API adapter.

These tests pin the *tolerance* of the parsers, not the correctness of the
field names — the names are unverified guesses and only `probe` can settle
them. What is asserted here is that an unexpected shape degrades to a
missing value rather than crashing a scheduled run or, worse, silently
parsing as a valid number.
"""

from __future__ import annotations

from cin7_reorder import schema
from cin7_reorder.models import PurchaseStatus


# ---------------------------------------------------------------------------
# Envelopes and coercion
# ---------------------------------------------------------------------------


def test_extract_list_finds_named_envelope():
    assert schema.extract_list({"Products": [{"ID": "1"}]}) == [{"ID": "1"}]


def test_extract_list_falls_back_to_any_list_of_dicts():
    """Cin7 adds endpoints faster than the envelope list gets updated."""
    assert schema.extract_list({"SomethingNew": [{"ID": "1"}]}) == [{"ID": "1"}]


def test_extract_list_handles_bare_lists_and_junk():
    assert schema.extract_list([{"ID": "1"}]) == [{"ID": "1"}]
    assert schema.extract_list({}) == []
    assert schema.extract_list(None) == []
    assert schema.extract_list("nonsense") == []


def test_get_first_is_case_insensitive():
    assert schema.get_first({"productid": "p1"}, "ProductID") == "p1"


def test_as_float_tolerates_strings_and_blanks():
    assert schema.as_float("12.5") == 12.5
    assert schema.as_float("") == 0.0
    assert schema.as_float(None) == 0.0
    assert schema.as_float("not a number") == 0.0


def test_as_optional_float_keeps_absent_distinct_from_zero():
    """Critical for reorder parameters.

    A lead time of 0 and a missing lead time mean very different things;
    conflating them would silently produce a par level of zero and stop the
    product ever being reordered.
    """
    assert schema.as_optional_float(0) == 0.0
    assert schema.as_optional_float(None) is None
    assert schema.as_optional_float("") is None


# ---------------------------------------------------------------------------
# Bills of materials
# ---------------------------------------------------------------------------


def test_parses_bom_from_a_product_record():
    """The shape confirmed against a live account.

    There is no /BillOfMaterials endpoint; every product carries its own
    under BillOfMaterialsProducts.
    """
    product = {
        "ID": "box-1",
        "SKU": "DW400-BOX",
        "BillOfMaterial": True,
        "BOMType": "Assembly",
        "BillOfMaterialsProducts": [{"ProductID": "sleeve-1", "Quantity": 24}],
    }
    assert schema.product_has_bom(product) is True

    parsed = schema.parse_bill_of_materials(product)
    assert parsed.parent_product_id == "box-1"
    assert parsed.components[0].component_product_id == "sleeve-1"
    assert parsed.components[0].quantity == 24


def test_product_without_a_bom_is_recognised():
    """The common case: BOMType None, empty component lists."""
    product = {
        "ID": "p1",
        "BOMType": "None",
        "BillOfMaterial": False,
        "BillOfMaterialsProducts": [],
        "BillOfMaterialsServices": [],
    }
    assert schema.product_has_bom(product) is False


def test_bom_flag_without_components_still_counts_as_having_one():
    """Worth surfacing rather than silently ignoring: the product is marked
    as an assembly, so an empty component list is a data problem."""
    assert schema.product_has_bom({"ID": "p1", "BillOfMaterial": True}) is True


def test_populated_components_win_over_a_false_flag():
    """Trust the data over the flag; an empty BOM is useless either way."""
    product = {
        "ID": "p1",
        "BillOfMaterial": False,
        "BOMType": "None",
        "BillOfMaterialsProducts": [{"ProductID": "c1", "Quantity": 6}],
    }
    assert schema.product_has_bom(product) is True


def test_parses_bom_components():
    parsed = schema.parse_bill_of_materials(
        {
            "ID": "box-1",
            "BOMComponents": [{"ProductID": "sleeve-1", "Quantity": 24}],
        }
    )
    assert parsed.parent_product_id == "box-1"
    assert parsed.components[0].component_product_id == "sleeve-1"
    assert parsed.components[0].quantity == 24


def test_bom_accepts_alternative_component_keys():
    parsed = schema.parse_bill_of_materials(
        {"ID": "box-1", "Components": [{"ComponentProductID": "s1", "Qty": 12}]}
    )
    assert parsed.components[0].component_product_id == "s1"
    assert parsed.components[0].quantity == 12


def test_bom_with_unknown_component_key_parses_empty_not_crashing():
    """The failure mode we want: no components, reported, not an exception."""
    parsed = schema.parse_bill_of_materials(
        {"ID": "box-1", "SomeUnexpectedKey": [{"ProductID": "s1", "Quantity": 24}]}
    )
    assert parsed is not None
    assert parsed.components == ()


def test_bom_drops_zero_quantity_components():
    parsed = schema.parse_bill_of_materials(
        {"ID": "box-1", "BOMComponents": [{"ProductID": "s1", "Quantity": 0}]}
    )
    assert parsed.components == ()


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------


def test_parses_status_values():
    assert schema.parse_status("AUTHORISED") is PurchaseStatus.AUTHORISED
    assert schema.parse_status("AUTHORIZED") is PurchaseStatus.AUTHORISED
    assert schema.parse_status("VOID") is PurchaseStatus.VOIDED
    assert schema.parse_status("something else") is PurchaseStatus.UNKNOWN
    assert schema.parse_status(None) is PurchaseStatus.UNKNOWN


def test_parses_combined_status_strings():
    """Cin7 list views combine statuses, e.g. 'AUTHORISED / PARTIALLY RECEIVED'."""
    assert (
        schema.parse_status("AUTHORISED / PARTIALLY RECEIVED")
        is PurchaseStatus.AUTHORISED
    )


def test_per_line_received_quantity_is_used_when_present():
    parsed = schema.parse_purchase(
        {
            "ID": "po-1",
            "OrderStatus": "AUTHORISED",
            "Location": "Main",
            "Order": {
                "Lines": [
                    {
                        "ProductID": "box-1",
                        "SKU": "BOX",
                        "Quantity": 10,
                        "ReceivedQuantity": 4,
                    }
                ]
            },
        }
    )
    line = parsed.lines[0]
    assert line.ordered_quantity == 10
    assert line.received_quantity == 4
    assert line.outstanding_quantity == 6


def test_receipts_are_summed_across_partial_receipt_lines():
    """Cin7 records partial receipts by appending stock-received lines.

    A product can legitimately appear several times and must be summed, not
    overwritten — otherwise only the last receipt counts and inbound reads
    too high.
    """
    parsed = schema.parse_purchase(
        {
            "ID": "po-1",
            "OrderStatus": "AUTHORISED",
            "Location": "Main",
            "Order": {"Lines": [{"ProductID": "box-1", "Quantity": 10}]},
            "StockReceived": [
                {"Lines": [{"ProductID": "box-1", "Quantity": 3}]},
                {"Lines": [{"ProductID": "box-1", "Quantity": 1}]},
            ],
        }
    )
    line = parsed.lines[0]
    assert line.received_quantity == 4
    assert line.outstanding_quantity == 6


def test_purchase_with_no_receipts_reads_as_nothing_received():
    parsed = schema.parse_purchase(
        {
            "ID": "po-1",
            "OrderStatus": "AUTHORISED",
            "Location": "Main",
            "Order": {"Lines": [{"ProductID": "box-1", "Quantity": 10}]},
        }
    )
    assert parsed.lines[0].received_quantity == 0
    assert parsed.lines[0].outstanding_quantity == 10


def test_purchase_without_id_is_rejected():
    assert schema.parse_purchase({"OrderStatus": "DRAFT"}) is None


# ---------------------------------------------------------------------------
# Supplier attributes
# ---------------------------------------------------------------------------


def test_reads_a_numbered_attribute_slot():
    """The shape confirmed against a live account.

    Cin7 returns AdditionalAttribute1..10 as flat fields; the readable label
    lives in the attribute set definition, not on the supplier.
    """
    supplier = {"ID": "s1", "Name": "Acme", "AdditionalAttribute3": "Yes"}
    assert schema.extract_supplier_attribute(supplier, "AdditionalAttribute3") == "Yes"


def test_empty_slot_reads_as_none_not_a_value():
    """Unset slots come back as empty strings.

    Treating that as a value would opt every supplier into automation, which
    is the exact failure the opt-in design exists to prevent.
    """
    supplier = {"ID": "s1", "AdditionalAttribute1": "", "AdditionalAttribute2": "   "}
    assert schema.extract_supplier_attribute(supplier, "AdditionalAttribute1") is None
    assert schema.extract_supplier_attribute(supplier, "AdditionalAttribute2") is None


def test_lists_populated_slots_for_the_probe():
    supplier = {
        "ID": "s1",
        "AdditionalAttribute1": "Yes",
        "AdditionalAttribute2": "",
        "AdditionalAttribute7": "weekly",
    }
    assert schema.supplier_attribute_slots(supplier) == {
        "AdditionalAttribute1": "Yes",
        "AdditionalAttribute7": "weekly",
    }


def test_no_populated_slots_is_an_empty_mapping():
    assert schema.supplier_attribute_slots({"ID": "s1", "Name": "Acme"}) == {}


def test_finds_attribute_in_a_name_value_list():
    value = schema.extract_supplier_attribute(
        {"AdditionalAttributes": [{"Name": "Auto Reorder", "Value": "Yes"}]},
        "Auto Reorder",
    )
    assert value == "Yes"


def test_finds_attribute_in_a_mapping():
    value = schema.extract_supplier_attribute(
        {"AdditionalAttributes": {"Auto Reorder": True}}, "Auto Reorder"
    )
    assert value is True


def test_finds_attribute_as_a_flat_key():
    value = schema.extract_supplier_attribute({"Auto Reorder": "yes"}, "Auto Reorder")
    assert value == "yes"


def test_missing_attribute_is_none_meaning_not_opted_in():
    assert schema.extract_supplier_attribute({"Name": "Acme"}, "Auto Reorder") is None


# ---------------------------------------------------------------------------
# Reorder parameters
# ---------------------------------------------------------------------------


def test_parses_product_level_reorder_point():
    parsed = schema.parse_reorder_parameters(
        {
            "ID": "prod-1",
            "DefaultSupplierID": "sup-1",
            "MinimumBeforeReorder": 100,
            "ReorderQuantity": 48,
        }
    )
    product_level = [p for p in parsed if p.location is None][0]
    assert product_level.minimum_before_reorder == 100
    assert product_level.reorder_quantity == 48
    assert product_level.supplier_id == "sup-1"
    assert product_level.is_complete is True


def test_parses_per_location_reorder_points():
    parsed = schema.parse_reorder_parameters(
        {
            "ID": "prod-1",
            "MinimumBeforeReorder": 100,
            "ReorderQuantity": 48,
            "ReorderLevels": [
                {"Location": "Main", "MinimumBeforeReorder": 30, "ReorderQuantity": 24}
            ],
        }
    )
    location_level = [p for p in parsed if p.location == "Main"][0]
    assert location_level.minimum_before_reorder == 30
    assert location_level.reorder_quantity == 24


def test_finds_location_blocks_nested_under_a_supplier():
    """Cin7's UI puts low-stock reorder points on the Suppliers tab.

    The API shape for that is unconfirmed, so both placements are checked.
    """
    parsed = schema.parse_reorder_parameters(
        {
            "ID": "prod-1",
            "Suppliers": [
                {
                    "SupplierID": "sup-1",
                    "Locations": [
                        {
                            "Location": "Main",
                            "MinimumBeforeReorder": 15,
                            "ReorderQuantity": 12,
                        }
                    ],
                }
            ],
        }
    )
    location_level = [p for p in parsed if p.location == "Main"][0]
    assert location_level.minimum_before_reorder == 15


def test_zero_minimum_parses_but_is_not_complete():
    """Cin7 defaults the field to 0; that must not read as a real trigger."""
    parsed = schema.parse_reorder_parameters(
        {"ID": "prod-1", "MinimumBeforeReorder": 0, "ReorderQuantity": 48}
    )
    assert parsed[0].minimum_before_reorder == 0
    assert parsed[0].is_complete is False


def test_missing_reorder_quantity_is_not_complete():
    parsed = schema.parse_reorder_parameters(
        {"ID": "prod-1", "MinimumBeforeReorder": 100}
    )
    assert parsed[0].minimum_before_reorder == 100
    assert parsed[0].reorder_quantity is None
    assert parsed[0].is_complete is False


def test_product_without_reorder_fields_yields_an_empty_entry():
    """Still returns a row, so the caller can distinguish "no reorder point"
    from "product not found"."""
    parsed = schema.parse_reorder_parameters({"ID": "prod-1"})
    assert len(parsed) == 1
    assert parsed[0].minimum_before_reorder is None


def test_product_without_id_yields_nothing():
    assert schema.parse_reorder_parameters({"MinimumBeforeReorder": 10}) == []
