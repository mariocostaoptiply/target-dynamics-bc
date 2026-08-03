"""Tests for purchase-order unit-of-measure conversion mapping."""

from typing import Any, cast

from target_dynamics_bc.mappers.purchase_order_schema_mapper import (
    PurchaseOrderSchemaMapper,
)


def _map_line(line_item: dict) -> dict:
    mapper = PurchaseOrderSchemaMapper.__new__(PurchaseOrderSchemaMapper)
    mapper.record = {"purchaseOrderLines": [line_item]}
    mapper.reference_data = cast(Any, {"Items": {"company-id": []}})
    mapper.company = {"id": "company-id"}

    return mapper._map_purchase_order_lines()[0]


def test_uses_purchase_values_when_converted_quantity_and_unit_are_present():
    mapped_line = _map_line(
        {
            "product_remoteId": "product-id",
            "quantity": 8448,
            "unitOfMeasureCode": "ROL",
            "directUnitCost": 0.175,
            "purchaseQuantity": 88,
            "purchaseUnitOfMeasureCode": "PAK",
            "purchaseDirectUnitCost": 16.81,
        }
    )

    assert mapped_line["quantity"] == 88
    assert mapped_line["unitOfMeasureCode"] == "PAK"
    assert mapped_line["directUnitCost"] == 16.81


def test_uses_base_values_when_purchase_values_are_absent():
    mapped_line = _map_line(
        {
            "product_remoteId": "product-id",
            "quantity": 20,
            "unitOfMeasureCode": "ROL",
            "directUnitCost": 0.97,
        }
    )

    assert mapped_line["quantity"] == 20
    assert mapped_line["unitOfMeasureCode"] == "ROL"
    assert mapped_line["directUnitCost"] == 0.97


def test_does_not_mix_partial_purchase_conversion_with_base_values():
    mapped_line = _map_line(
        {
            "product_remoteId": "product-id",
            "quantity": 20,
            "unitOfMeasureCode": "ROL",
            "directUnitCost": 0.97,
            "purchaseQuantity": 1,
            "purchaseDirectUnitCost": 19.4,
        }
    )

    assert mapped_line["quantity"] == 20
    assert mapped_line["unitOfMeasureCode"] == "ROL"
    assert mapped_line["directUnitCost"] == 0.97


def test_does_not_use_base_cost_when_purchase_cost_is_absent():
    mapped_line = _map_line(
        {
            "product_remoteId": "product-id",
            "quantity": 20,
            "unitOfMeasureCode": "ROL",
            "directUnitCost": 0.97,
            "purchaseQuantity": 1,
            "purchaseUnitOfMeasureCode": "DOOS",
        }
    )

    assert mapped_line["quantity"] == 1
    assert mapped_line["unitOfMeasureCode"] == "DOOS"
    assert "directUnitCost" not in mapped_line


def test_preserves_zero_converted_values():
    mapped_line = _map_line(
        {
            "product_remoteId": "product-id",
            "quantity": 10,
            "unitOfMeasureCode": "ROL",
            "purchaseQuantity": 0,
            "purchaseUnitOfMeasureCode": "DOOS",
            "purchaseDirectUnitCost": 0,
        }
    )

    assert mapped_line["quantity"] == 0
    assert mapped_line["unitOfMeasureCode"] == "DOOS"
    assert mapped_line["directUnitCost"] == 0
