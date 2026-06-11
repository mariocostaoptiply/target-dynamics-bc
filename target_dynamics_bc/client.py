import json
import requests

from target_dynamics_bc.mappers.base_mappers import BaseMapper
from target_hotglue.common import HGJSONEncoder
from typing import Dict, List, Optional, Tuple
import singer


from target_dynamics_bc.auth import DynamicsAuth
from target_dynamics_bc.utils import extract_error_message

LOGGER = singer.get_logger()


class DynamicsClient:
    ref_request_endpoints = {
        "Companies": "companies",
        "Accounts": "companies({companyId})/accounts",
        "Locations": "companies({companyId})/locations",
        "Items": "companies({companyId})/items",
        "Currencies": "companies({companyId})/currencies",
        "PaymentMethods": "companies({companyId})/paymentMethods",
        "Customers": "companies({companyId})/customers",
        "Vendors": "companies({companyId})/vendors",
        "Dimensions": "companies({companyId})/dimensions",
        "purchaseInvoices": "companies({companyId})/purchaseInvoices",
        "purchaseInvoicesDimensionSetLines": "companies({companyId})/purchaseInvoices({entityId})/dimensionSetLines",
        "purchaseInvoiceLinesDimensionSetLines": "companies({companyId})/purchaseInvoiceLines({entityId})/dimensionSetLines",
        "purchaseInvoiceLines": "companies({companyId})/purchaseInvoices({parentId})/purchaseInvoiceLines",
        "purchaseOrders": "companies({companyId})/purchaseOrders",
        "purchaseOrdersDimensionSetLines": "companies({companyId})/purchaseOrders({entityId})/dimensionSetLines",
        "purchaseOrderLinesDimensionSetLines": "companies({companyId})/purchaseOrderLines({entityId})/dimensionSetLines",
        "purchaseOrderLines": "companies({companyId})/purchaseOrders({parentId})/purchaseOrderLines",
        "Journals": "companies({companyId})/journals",
        "vendorPaymentJournals": "companies({companyId})/vendorPaymentJournals",
        "vendorPayments": "companies({companyId})/vendorPaymentJournals({parentId})/vendorPayments",
        "vendorPaymentsDimensionSetLines": "companies({companyId})/vendorPaymentJournals({parentId})/vendorPayments({entityId})/dimensionSetLines",
    }

    def __init__(self, target) -> None:
        self.config = target.config
        environment = self.config.get("environment_name")
        self.url = self.config.get(
            "full_url",
            f"https://api.businesscentral.dynamics.com/v2.0/{environment}/api/v2.0/",
        )
        self.auth = DynamicsAuth(target)

    def get_auth(self):
        r = requests.Session()
        return self.auth(r)

    def _make_request(self, endpoint, method, data=None, params=None, headers=None):
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)

        url = self.url + endpoint
        request_params = params or {}

        request = self.get_auth()
        request.headers.update(request_headers)

        json_data = json.dumps(data, cls=HGJSONEncoder) if data else None

        return request.request(
            method=method, url=url, params=request_params, data=json_data, verify=True
        )

    def _validate_response(
        self, response: requests.Response
    ) -> Tuple[bool, Optional[str]]:
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            msg = extract_error_message({"body": body})
            return False, msg
        else:
            return True, None

    def _validate_batch_response(self, response: dict) -> Tuple[bool, Optional[str]]:
        if response["status"] >= 400:
            msg = extract_error_message(response)
            return False, msg
        else:
            return True, None

    def make_batch_request(
        self, requests_data: List[dict], transaction_type: str = "non_atomic"
    ):
        """
        Performs a batch request against the API, any API endpoint can be used in batch requests.
        requests_data: list of requests, each containing a dict of url, method, headers and body
        transaction_type:
            'non_atomic' = the batch requests will continue to be processed even if one of them fail
            'atomic' = if one of the requests fails all the other requests will be rolled back. It's good
                        to be used when multiple requests are needed for one entity, for example updating
                        Customer and it's default dimensions
        """
        headers = {"Prefer": "odata.continue-on-error=true"}

        if transaction_type == "atomic":
            # Prefer: odata.continue-on-error=false makes the batch request stop processing requests if one of them fail
            # Isolation: snapshot makes the batch request atomic, if one of the requests fail the operation will be rolled back
            # it's good to be used when multiple requests are needed for one entity, for example updating Customer and it's default dimensions
            headers = {
                "Isolation": "snapshot",
                "Prefer": "odata.continue-on-error=false",
            }

        request_data = {"requests": []}

        for request in requests_data:
            req_headers = request.get("headers", {})
            data = {
                "method": request["method"],
                "url": request["url"],
                "headers": {
                    "Content-Type": "application/json",
                    "If-Match": "*",
                    **req_headers,
                },
                "body": request.get("body", {}),
            }
            request_id = request.get("request_id")
            if request_id:
                data["id"] = request_id

            request_data["requests"].append(data)

        response = self._make_request(
            "$batch", "POST", data=request_data, headers=headers
        )
        responses = response.json().get("responses", [])
        return responses

    def get_entities(
        self,
        record_type: str,
        url_params: Optional[dict] = {},
        filters: Optional[Dict[str, List]] = {},
        expand: str = None,
    ):
        """ "Uses batch request to get data because the url can be of any length, allowing for long filters"""
        endpoint = self.ref_request_endpoints[record_type].format(**url_params)
        entity_filters = []

        if expand:
            expand = f"$expand={expand}"

        for filter_field_name, filter_values in filters.items():
            if filter_values:
                entity_filters.append(
                    [
                        f"{filter_field_name} eq {filter_value}"
                        for filter_value in filter_values
                    ]
                )

        if not entity_filters:
            entity_filters = [[]]

        requests_data = []

        # Dynamics doesn't support filtering on distinct fields, so we need to make one request for each filter type
        for entity_filter in entity_filters:
            if entity_filter:
                entity_filter = f"$filter={' or '.join(entity_filter)}"

            request_url = endpoint
            query_string = "&".join(filter(None, [expand, entity_filter]))
            if query_string:
                request_url += f"?{query_string}"

            requests_data.append(
                {
                    "url": request_url,
                    "method": "GET",
                }
            )

        batch_responses = self.make_batch_request(requests_data)

        entities = []

        for response in batch_responses:
            success, error_message = self._validate_batch_response(response)
            if not success:
                return success, error_message, entities
            entities += response.get("body", {}).get("value", [])

        return True, None, entities

    def get_companies(self):
        _, _, companies = self.get_entities("Companies")

        for company in companies:
            url_params = {"companyId": company["id"]}

            _, _, currencies = self.get_entities("Currencies", url_params)
            company["currencies"] = currencies

            _, _, payment_methods = self.get_entities("PaymentMethods", url_params)
            company["paymentMethods"] = payment_methods

            _, _, dimensions = self.get_entities(
                "Dimensions", url_params, expand="dimensionValues"
            )
            company["dimensions"] = dimensions

            _, _, accounts = self.get_entities("Accounts", url_params)
            company["accounts"] = accounts

            _, _, locations = self.get_entities("Locations", url_params)
            company["locations"] = locations

        return True, None, companies

    def get_existing_entities_for_records(
        self,
        companies_reference_data: List[Dict],
        record_type: str,
        records: List[Dict],
        filter_mappings: List[Dict],
        expand: Optional[str] = None,
    ) -> Dict[str, List]:
        """Maps records to companies and returns a list of entities based on 'records'"""

        # we need to map the company to query the existing customers
        company_entities_mapping = {}

        for record in records:
            company = BaseMapper.get_company_from_record(
                companies_reference_data, record
            )
            if not company:
                continue

            if company["id"] not in company_entities_mapping.keys():
                company_entities_mapping[company["id"]] = {}

            for filter_mapping in filter_mappings:
                filter_field_from = filter_mapping["field_from"]
                filter_field_to = filter_mapping["field_to"]
                filter_field_should_quote = filter_mapping["should_quote"]

                if filter_field_to not in company_entities_mapping[company["id"]]:
                    company_entities_mapping[company["id"]][filter_field_to] = []

                rec_value = record.get(filter_field_from)
                if rec_value:
                    # escape odata string
                    rec_value = DynamicsClient.escape_odata_string(rec_value)
                    if filter_field_should_quote:
                        rec_value = f"'{rec_value}'"
                    company_entities_mapping[company["id"]][filter_field_to].append(
                        rec_value
                    )

        existing_company_entities = {}
        # make requests to get existing entities for each company from Dynamics
        for company_id in company_entities_mapping:
            url_params = {"companyId": company_id}

            has_filters_to_apply = False
            for filter_values in company_entities_mapping[company_id].values():
                if filter_values:
                    has_filters_to_apply = True

            entities = []
            if has_filters_to_apply:
                _, _, entities = self.get_entities(
                    record_type,
                    url_params=url_params,
                    filters=company_entities_mapping[company_id],
                    expand=expand,
                )

            if company_id not in existing_company_entities.keys():
                existing_company_entities[company_id] = []
            existing_company_entities[company_id] += entities

        return existing_company_entities

    def get_existing_bill_payments_for_records(
        self,
        companies_reference_data: List[Dict],
        company_payment_journals: Dict[str, List],
        records: List[Dict],
        filter_mappings: List[Dict],
    ) -> Dict[str, List]:
        """Maps records to companies and returns a list of entities based on 'records'"""

        # we need to map the company to query the existing entities
        company_entities_mapping = {}

        for record in records:
            company = BaseMapper.get_company_from_record(
                companies_reference_data, record
            )
            if not company:
                continue

            if company["id"] not in company_entities_mapping.keys():
                company_entities_mapping[company["id"]] = {}

            for filter_mapping in filter_mappings:
                filter_field_from = filter_mapping["field_from"]
                filter_field_to = filter_mapping["field_to"]
                filter_field_should_quote = filter_mapping["should_quote"]

                if filter_field_to not in company_entities_mapping[company["id"]]:
                    company_entities_mapping[company["id"]][filter_field_to] = []

                rec_value = record.get(filter_field_from)
                if rec_value:
                    if filter_field_should_quote:
                        rec_value = f"'{rec_value}'"
                    company_entities_mapping[company["id"]][filter_field_to].append(
                        rec_value
                    )

        existing_company_bill_payments = {}
        # make requests to get existing entities for each company from Dynamics
        for company_id in company_entities_mapping:
            url_params = {"companyId": company_id}
            for payment_journal in company_payment_journals.get(company_id, []):
                url_params["parentId"] = payment_journal["id"]
                _, _, entities = self.get_entities(
                    "vendorPayments",
                    url_params=url_params,
                    filters=company_entities_mapping[company_id],
                )
                if company_id not in existing_company_bill_payments.keys():
                    existing_company_bill_payments[company_id] = []
                existing_company_bill_payments[company_id] += entities

        return existing_company_bill_payments

    @staticmethod
    def create_default_dimensions_requests(
        record_type: str,
        company_id: str,
        entity_id: str,
        default_dimensions: List[dict],
    ):
        """
        If the Entity already exists we cannot create/update defaultDimensions for it.
        We need to send a separate request for it
        """
        requests = []

        for default_dimension in default_dimensions:
            endpoint = (
                DynamicsClient.ref_request_endpoints[record_type]
                + "({entityId})/defaultDimensions"
            )
            endpoint = endpoint.format(companyId=company_id, entityId=entity_id)
            request_params = {"url": endpoint, "method": "POST"}

            default_dimension_id = default_dimension.pop("id", None)
            if default_dimension_id:
                request_params = {
                    "url": f"{endpoint}({default_dimension_id})",
                    "method": "PATCH",
                }
            requests.append(
                {"payload": default_dimension, "request_params": request_params}
            )

        return requests

    @staticmethod
    def create_dimension_set_lines_requests(
        record_type: str,
        company_id: str,
        entity_id: str,
        dimensions_set_lines: List[dict],
        existing_dimension_set_lines: Optional[List[dict]] = [],
        parentId: Optional[str] = None,
    ):
        """
        Create the requests for upserting dimension set lines for a given entity
        """
        requests = []

        for dimension_set_line in dimensions_set_lines:
            dimension_id = dimension_set_line["id"]
            dimension_value_id = dimension_set_line["valueId"]

            found_existing_dimension = next(
                (
                    existing_dimension
                    for existing_dimension in existing_dimension_set_lines
                    if existing_dimension["id"] == dimension_id
                ),
                None,
            )

            endpoint = DynamicsClient.ref_request_endpoints[record_type]
            endpoint = endpoint.format(
                companyId=company_id, entityId=entity_id, parentId=parentId
            )

            if not found_existing_dimension:
                request_params = {"url": endpoint, "method": "POST"}
                body = {"id": dimension_id, "valueId": dimension_value_id}
            else:
                request_params = {
                    "url": f"{endpoint}({dimension_id})",
                    "method": "PATCH",
                }
                body = {"valueId": dimension_value_id}

            requests.append({**request_params, "body": body})

        return requests

    @staticmethod
    def get_entity_upsert_request_params(
        record_type: str,
        company_id: str,
        entity_id: str = None,
        url_params: Optional[dict] = {},
        request_id: str = None,
    ):
        endpoint = DynamicsClient.ref_request_endpoints[record_type]
        endpoint = endpoint.format(companyId=company_id, **url_params)
        request_params = {"url": endpoint, "method": "POST"}

        if entity_id:
            request_params = {"url": f"{endpoint}({entity_id})", "method": "PATCH"}

        if request_id:
            request_params["request_id"] = request_id

        return request_params

    @staticmethod
    def escape_odata_string(value: Optional[str]) -> str:
        """Escape single quotes in OData string literals by doubling them."""
        if value is None:
            return ""
        return str(value).replace("'", "''")
