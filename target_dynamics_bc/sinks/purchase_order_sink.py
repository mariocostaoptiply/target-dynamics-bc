from singer_sdk import typing as th

from target_dynamics_bc.client import DynamicsClient
from target_dynamics_bc.mappers.purchase_order_schema_mapper import (
    PurchaseOrderSchemaMapper,
)
from target_dynamics_bc.sinks.base_sinks import DynamicsBaseBatchSinkSingleUpsert
from target_dynamics_bc.utils import InvalidRecordState, extract_error_message


class PurchaseOrderSink(DynamicsBaseBatchSinkSingleUpsert):
    name = "BuyOrders"
    record_type = "purchaseOrders"
    auto_validate_unified_schema = False
    schema = th.PropertiesList(
        th.Property("id", th.StringType),
        th.Property("externalid", th.StringType),
        th.Property("externalId", th.StringType),
        th.Property("supplier_remoteId", th.StringType),
        th.Property("supplier_name", th.StringType),
        th.Property("vendorId", th.StringType),
        th.Property("vendorNumber", th.StringType),
        th.Property("transaction_date", th.StringType),
        th.Property("created_at", th.StringType),
        th.Property("line_items", th.CustomType({"type": ["array", "string", "null"]})),
        th.Property("purchaseOrderLines", th.ArrayType(th.ObjectType())),
    ).to_dict()

    def preprocess_batch(self, records: list[dict]):
        purchase_order_filter_mappings = [
            {"field_from": "id", "field_to": "id", "should_quote": False},
            {"field_from": "number", "field_to": "number", "should_quote": True},
            {
                "field_from": "transactionNumber",
                "field_to": "number",
                "should_quote": True,
            },
        ]
        existing_company_purchase_orders = self.dynamics_client.get_existing_entities_for_records(
            self._target.reference_data.get("companies", []),
            self.record_type,
            records,
            purchase_order_filter_mappings,
            expand="dimensionSetLines, purchaseOrderLines($expand=dimensionSetLines)",
        )

        vendor_filter_mappings = [
            {"field_from": "vendorId", "field_to": "id", "should_quote": False},
            {
                "field_from": "supplier_remoteId",
                "field_to": "id",
                "should_quote": False,
            },
            {"field_from": "vendorNumber", "field_to": "number", "should_quote": True},
            {
                "field_from": "vendorName",
                "field_to": "displayName",
                "should_quote": True,
            },
            {
                "field_from": "supplier_name",
                "field_to": "displayName",
                "should_quote": True,
            },
        ]
        existing_company_vendors = (
            self.dynamics_client.get_existing_entities_for_records(
                self._target.reference_data.get("companies", []),
                "Vendors",
                records,
                vendor_filter_mappings,
            )
        )

        items = []
        seen_items = set()
        for record in records:
            try:
                for line_item in PurchaseOrderSchemaMapper(
                    record,
                    self,
                    {**self._target.reference_data, self.name: {}, "Vendors": {}},
                )._parse_line_items():
                    item = (
                        line_item.get("itemId") or line_item.get("product_remoteId"),
                        line_item.get("itemNumber")
                        or line_item.get("lineObjectNumber")
                        or line_item.get("sku"),
                        line_item.get("itemName") or line_item.get("description"),
                        record.get("subsidiaryId"),
                        record.get("subsidiaryName"),
                    )
                    if item not in seen_items:
                        seen_items.add(item)
                        items.append(
                            {
                                "itemId": item[0],
                                "itemNumber": item[1],
                                "itemName": item[2],
                                "subsidiaryId": item[3],
                                "subsidiaryName": item[4],
                            }
                        )
            except Exception:
                continue

        item_filter_mappings = [
            {"field_from": "itemId", "field_to": "id", "should_quote": False},
            {"field_from": "itemNumber", "field_to": "number", "should_quote": True},
            {"field_from": "itemName", "field_to": "displayName", "should_quote": True},
        ]
        existing_company_items = (
            self.dynamics_client.get_existing_entities_for_records(
                self._target.reference_data.get("companies", []),
                "Items",
                items,
                item_filter_mappings,
            )
            if items
            else []
        )

        self.reference_data = {
            **self._target.reference_data,
            self.name: existing_company_purchase_orders,
            "Vendors": existing_company_vendors,
            "Items": existing_company_items,
        }

    def process_batch_record(self, record: dict) -> dict:
        if record.get("externalid") and not record.get("externalId"):
            record["externalId"] = record["externalid"]
        mapped_record = PurchaseOrderSchemaMapper(
            record, self, self.reference_data
        ).to_dynamics()
        mapped_record["source_external_id"] = record.get("externalId") or record.get(
            "id"
        )
        return mapped_record

    def _get_buy_order_error_id(self, record: dict | None = None) -> str:
        if not record:
            return "unknown"
        return str(record.get("source_external_id") or record.get("id") or "unknown")

    def _set_error(
        self, state: dict, stage: str, response: dict, record: dict | None = None
    ):
        error_message = extract_error_message(response)
        buy_order_id = self._get_buy_order_error_id(record)
        state["error"] = f"Error BuyOrder {buy_order_id}: {stage} failed"
        if response.get("status"):
            state["error"] += f" status={response['status']}"
        if error_message:
            state["error"] += f" - {error_message}"

    def upsert_record(self, record: dict) -> tuple[str, bool, dict]:
        state = {}
        payload = record["payload"]
        company_id = record["company_id"]
        purchase_order_id = payload.pop("id", None)
        is_update = purchase_order_id is not None
        purchase_order_dimensions = payload.pop("dimensionSetLines", [])
        purchase_order_lines = payload.pop("purchaseOrderLines", [])

        if is_update and record.get("status") not in [None, "Draft", "Open"]:
            raise InvalidRecordState(
                "Cannot update a Purchase Order that's not in Draft/Open state"
            )

        if purchase_order_id:
            request_params = DynamicsClient.get_entity_upsert_request_params(
                self.record_type, company_id, purchase_order_id
            )
        else:
            request_params = DynamicsClient.get_entity_upsert_request_params(
                self.record_type, company_id
            )
        purchase_order_upsert_response = self.dynamics_client.make_batch_request(
            [{**request_params, "body": payload}]
        )[0]

        if purchase_order_upsert_response.get("status") not in [200, 201]:
            self._set_error(
                state, "header upsert", purchase_order_upsert_response, record
            )
            return purchase_order_id or "", False, state

        purchase_order_id = purchase_order_upsert_response["body"]["id"]

        if purchase_order_dimensions:
            _, _, purchase_orders = self.dynamics_client.get_entities(
                self.record_type,
                url_params={"companyId": company_id},
                filters={"id": [purchase_order_id]},
                expand="dimensionSetLines",
            )
            upserted_purchase_order = purchase_orders[0] if purchase_orders else {}
            existing_dimensions = upserted_purchase_order.get("dimensionSetLines", [])
            dimension_requests = DynamicsClient.create_dimension_set_lines_requests(
                "purchaseOrdersDimensionSetLines",
                company_id,
                purchase_order_id,
                purchase_order_dimensions,
                existing_dimensions,
            )
            dimension_responses = self.dynamics_client.make_batch_request(
                dimension_requests
            )
            for dimension_response in dimension_responses:
                if dimension_response.get("status") not in [200, 201]:
                    self._set_error(
                        state, "dimension upsert", dimension_response, record
                    )
                    return purchase_order_id or "", False, state

        if not purchase_order_lines:
            if is_update:
                state["is_updated"] = True
            return purchase_order_id or "", True, state

        line_upsert_requests = []
        line_metadata = []
        url_params = {"parentId": purchase_order_id}
        for index, purchase_order_line in enumerate(purchase_order_lines):
            purchase_order_line_id = purchase_order_line.pop("id", None)
            line_external_id = (
                purchase_order_line.pop("_externalId", None)
                or purchase_order_line_id
                or index + 1
            )
            request_id = purchase_order_line_id or f"line_{index}"
            purchase_order_line.pop("dimensionSetLines", None)
            line_metadata.append(
                {
                    "external_id": str(line_external_id),
                    "item": purchase_order_line.get("lineObjectNumber")
                    or purchase_order_line.get("itemId"),
                    "quantity": purchase_order_line.get("quantity"),
                }
            )
            request_params = DynamicsClient.get_entity_upsert_request_params(
                "purchaseOrderLines",
                company_id,
                entity_id=purchase_order_line_id,
                url_params=url_params,
                request_id=request_id,
            )
            line_upsert_requests.append({**request_params, "body": purchase_order_line})

        line_upsert_responses = (
            self.dynamics_client.make_batch_request(line_upsert_requests)
            if line_upsert_requests
            else []
        )
        failed_line_ids = []
        failed_line_details = []
        for index, line_upsert_response in enumerate(line_upsert_responses):
            if line_upsert_response.get("status") not in [200, 201]:
                metadata = (
                    line_metadata[index]
                    if index < len(line_metadata)
                    else {"external_id": str(index + 1)}
                )
                failed_line_ids.append(metadata["external_id"])
                failed_line_details.append(
                    "line {line_id} item={item} quantity={quantity} status={status} message={message}".format(
                        line_id=metadata["external_id"],
                        item=metadata.get("item"),
                        quantity=metadata.get("quantity"),
                        status=line_upsert_response.get("status"),
                        message=extract_error_message(line_upsert_response),
                    )
                )

        if failed_line_ids:
            buy_order_id = self._get_buy_order_error_id(record)
            state["error"] = (
                f"Error BuyOrder {buy_order_id} on lines {','.join(failed_line_ids)}"
            )
            self.logger.error("%s: %s", state["error"], " ; ".join(failed_line_details))
            return purchase_order_id or "", False, state

        if is_update:
            state["is_updated"] = True

        return purchase_order_id or "", True, state


class PurchaseOrdersSink(PurchaseOrderSink):
    name = "PurchaseOrders"
