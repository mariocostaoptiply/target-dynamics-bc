import json
from datetime import datetime
from typing import Any, cast

from target_dynamics_bc.mappers.base_mappers import BaseMapper
from target_dynamics_bc.utils import InvalidInputError, RecordNotFound


class PurchaseOrderSchemaMapper(BaseMapper):
    name = "BuyOrders"
    existing_record_pk_mappings = [
        {
            "record_field": "dynamicsId",
            "dynamics_field": "id",
            "required_if_present": True,
        },
        {
            "record_field": "number",
            "dynamics_field": "number",
            "required_if_present": False,
        },
        {
            "record_field": "transactionNumber",
            "dynamics_field": "number",
            "required_if_present": False,
        },
    ]

    def _get_optiply_id(self) -> str | None:
        optiply_id = (
            self.record.get("externalId")
            or self.record.get("externalid")
            or self.record.get("id")
        )
        return str(optiply_id) if optiply_id else None

    def _should_export_reference_number(self) -> bool:
        return bool(
            getattr(getattr(self.sink, "_target", None), "export_reference_number", False)
        )

    def _get_purchase_order_number(self) -> str | None:
        if not self._should_export_reference_number():
            return None

        number = self.record.get("number") or self.record.get("transactionNumber")
        if number:
            return str(number)

        optiply_id = self._get_optiply_id()
        if optiply_id:
            return f"OP-{optiply_id}"

        return None

    def _map_company(self) -> dict:
        companies = self.reference_data.get("companies", [])
        export_company_id = getattr(
            getattr(self.sink, "_target", None), "export_company_id", None
        )
        company = next(
            (
                company
                for company in companies
                if export_company_id and company.get("id") == export_company_id
            ),
            None,
        )
        return cast(dict, company)

    def _find_existing_record(self, reference_list):
        if not self.company:
            return None

        existing_entities_in_dynamics = reference_list.get(self.company["id"], [])

        purchase_order_number = self._get_purchase_order_number()
        if purchase_order_number:
            found_record = next(
                (
                    dynamics_record
                    for dynamics_record in existing_entities_in_dynamics
                    if dynamics_record.get("number") == purchase_order_number
                ),
                None,
            )
            if found_record:
                return found_record

        for existing_record_pk_mapping in self.existing_record_pk_mappings:
            if (
                existing_record_pk_mapping["dynamics_field"] == "number"
                and not self._should_export_reference_number()
            ):
                continue
            record_id = self.record.get(existing_record_pk_mapping["record_field"])
            if not record_id:
                continue

            found_record = next(
                (
                    dynamics_record
                    for dynamics_record in existing_entities_in_dynamics
                    if dynamics_record.get(existing_record_pk_mapping["dynamics_field"])
                    == record_id
                ),
                None,
            )
            if (
                existing_record_pk_mapping["required_if_present"]
                and found_record is None
            ):
                raise RecordNotFound(
                    f"Purchase Order {existing_record_pk_mapping['record_field']}={record_id} not found in Dynamics. Skipping it"
                )

            if found_record:
                return found_record

        return None

    def _format_date(self, value: Any) -> str | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        value = str(value)
        if not value or value.lower() == "nan":
            return None
        return value[:10]

    def _parse_line_items(self) -> list[dict]:
        line_items = self.record.get("purchaseOrderLines")
        if line_items is None:
            line_items = self.record.get("line_items")
        if line_items is None:
            line_items = self.record.get("lineItems")
        if line_items is None:
            line_items = []

        if isinstance(line_items, str):
            if not line_items:
                return []
            try:
                line_items = json.loads(line_items)
            except json.JSONDecodeError as exc:
                raise InvalidInputError(f"Invalid JSON in line_items: {exc}") from exc

        if not isinstance(line_items, list):
            raise InvalidInputError("line_items must be a list or a JSON encoded list")

        return line_items

    def _map_vendor(self, required: bool = False):
        vendor_info = {}
        vendors_reference_data = self.reference_data.get("Vendors", {}).get(
            self.company["id"], []
        )

        vendor_id = self.record.get("vendorId") or self.record.get("supplier_remoteId")
        vendor_number = self.record.get("vendorNumber") or self.record.get(
            "supplierNumber"
        )
        vendor_name = self.record.get("vendorName") or self.record.get("supplier_name")

        found_vendor = None
        if vendor_id:
            found_vendor = next(
                (
                    vendor
                    for vendor in vendors_reference_data
                    if vendor["id"] == vendor_id
                ),
                None,
            )
        if vendor_number and not found_vendor:
            found_vendor = next(
                (
                    vendor
                    for vendor in vendors_reference_data
                    if vendor["number"] == vendor_number
                ),
                None,
            )
        if vendor_name and not found_vendor:
            found_vendor = next(
                (
                    vendor
                    for vendor in vendors_reference_data
                    if vendor["displayName"] == vendor_name
                ),
                None,
            )

        if found_vendor:
            vendor_info = {"vendorId": found_vendor["id"]}
        elif vendor_id:
            vendor_info = {"vendorId": vendor_id}
        elif vendor_number:
            vendor_info = {"vendorNumber": vendor_number}

        if required:
            if vendor_id is None and vendor_number is None and vendor_name is None:
                raise InvalidInputError(
                    "Vendor not informed. Please provide one of vendorId / vendorNumber / vendorName / supplier_remoteId / supplier_name"
                )
            if not vendor_info:
                raise RecordNotFound(
                    f"Vendor not found for vendorId={vendor_id} / vendorNumber={vendor_number} / vendorName={vendor_name}"
                )

        return vendor_info

    def _map_purchase_order_lines(self) -> list[dict]:
        mapped_lines = []
        items_reference_data = self.reference_data.get("Items", {}).get(
            self.company["id"], []
        )

        for index, line_item in enumerate(self._parse_line_items()):
            if not isinstance(line_item, dict):
                raise InvalidInputError(
                    f"Line {index + 1}: expected an object, got {type(line_item).__name__}"
                )

            item_id = line_item.get("itemId") or line_item.get("product_remoteId")
            item_number = (
                line_item.get("itemNumber")
                or line_item.get("lineObjectNumber")
                or line_item.get("sku")
            )
            item_name = line_item.get("itemName") or line_item.get("description")

            found_item = None
            if item_id:
                found_item = next(
                    (item for item in items_reference_data if item["id"] == item_id),
                    None,
                )
            if item_number and not found_item:
                found_item = next(
                    (
                        item
                        for item in items_reference_data
                        if item["number"] == item_number
                    ),
                    None,
                )
            if item_name and not found_item:
                found_item = next(
                    (
                        item
                        for item in items_reference_data
                        if item["displayName"] == item_name
                    ),
                    None,
                )

            line_payload = {
                "lineType": line_item.get("lineType", "Item"),
                "quantity": line_item.get("quantity"),
            }

            if found_item:
                line_payload["itemId"] = found_item["id"]
                line_payload["lineObjectNumber"] = found_item["number"]
            elif item_id:
                line_payload["itemId"] = item_id
            elif item_number:
                line_payload["lineObjectNumber"] = item_number
            else:
                raise InvalidInputError(
                    f"Line {index + 1}: item not informed. Please provide product_remoteId / itemId / sku / lineObjectNumber"
                )

            if line_payload["quantity"] in [None, ""]:
                raise InvalidInputError(f"Line {index + 1}: quantity is required")

            direct_unit_cost = (
                line_item.get("directUnitCost")
                or line_item.get("unit_price")
                or line_item.get("unitCost")
            )
            if direct_unit_cost not in [None, ""]:
                line_payload["directUnitCost"] = direct_unit_cost

            description = line_item.get("description") or line_item.get("name")
            if description:
                line_payload["description"] = description

            expected_receipt_date = self._format_date(
                line_item.get("expectedReceiptDate")
                or line_item.get("expected_receipt_date")
            )
            if expected_receipt_date:
                line_payload["expectedReceiptDate"] = expected_receipt_date

            line_external_id = (
                line_item.get("externalId")
                or line_item.get("externalid")
                or line_item.get("line_id")
                or line_item.get("id")
            )
            if line_external_id:
                line_payload["_externalId"] = line_external_id

            location_id = line_item.get("locationId")
            if location_id:
                line_payload["locationId"] = location_id

            mapped_lines.append(line_payload)

        return mapped_lines

    def to_dynamics(self) -> dict:
        self._validate_company()

        payload = {
            **self._map_internal_id(),
            **self._map_vendor(required=True),
            **self._map_currency(),
            **self._map_dimension_set_lines(),
        }

        order_date = self._format_date(
            self.record.get("orderDate") or self.record.get("transaction_date")
        )
        if order_date:
            payload["orderDate"] = order_date
            payload["postingDate"] = (
                self._format_date(self.record.get("postingDate")) or order_date
            )

        requested_receipt_date = self._format_date(
            self.record.get("requestedReceiptDate")
            or self.record.get("created_at")
            or self.record.get("delivery_date")
        )
        if requested_receipt_date:
            payload["requestedReceiptDate"] = requested_receipt_date

        purchase_order_number = self._get_purchase_order_number()
        if purchase_order_number and not self.existing_record:
            payload["number"] = purchase_order_number

        purchase_order_lines = self._map_purchase_order_lines()
        if purchase_order_lines:
            payload["purchaseOrderLines"] = purchase_order_lines

        status = self.existing_record["status"] if self.existing_record else None

        return {
            "payload": payload,
            "id": payload.get("id"),
            "company_id": self.company["id"],
            "status": status,
        }
