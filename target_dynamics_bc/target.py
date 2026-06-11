"""DynamicsV2 target class."""

import json
import os

from singer_sdk import typing as th
from target_hotglue.target import TargetHotglue

from target_dynamics_bc.client import DynamicsClient
from target_dynamics_bc.sinks.bill_payment_sink import BillPaymentSink
from target_dynamics_bc.sinks.bill_sink import BillSink
from target_dynamics_bc.sinks.customer_sink import CustomerSink
from target_dynamics_bc.sinks.journal_entry_sink import JournalEntrySink
from target_dynamics_bc.sinks.purchase_order_sink import (
    PurchaseOrderSink,
    PurchaseOrdersSink,
)
from target_dynamics_bc.sinks.vendor_sink import VendorSink
from target_dynamics_bc.utils import (
    ReferenceData,
    DimensionDefinitionNotFound,
    InvalidConfigurationError,
)


class TargetDynamicsV2(TargetHotglue):
    """Sample target for DynamicsV2."""

    SINK_TYPES = [
        CustomerSink,
        VendorSink,
        BillSink,
        BillPaymentSink,
        JournalEntrySink,
        PurchaseOrderSink,
        PurchaseOrdersSink,
    ]
    name = "target-dynamics-bc"

    def __init__(
        self,
        config=None,
        parse_env_config: bool = False,
        validate_config: bool = True,
        state: str = None,
    ) -> None:
        self.config_file = config[0]
        super().__init__(
            config=config,
            parse_env_config=parse_env_config,
            validate_config=validate_config,
        )

        self.export_company_id = self.get_export_company_id()
        self.dynamics_client = DynamicsClient(self)
        self.reference_data: ReferenceData = self.get_reference_data()
        self.validate_export_company_id()
        self.dimensions_mapping = self.load_fields_and_dimensions_mapping_config()

    def get_export_company_id(self) -> str:
        export_company_id = self.config.get("export_company_id")
        if not export_company_id:
            raise InvalidConfigurationError(
                "export_company_id is required in the target config. "
                "Set it to the Business Central company id that should receive exported Buy Orders."
            )
        return str(export_company_id)

    def validate_export_company_id(self):
        found_company = next(
            (
                company
                for company in self.reference_data["companies"]
                if company["id"] == self.export_company_id
            ),
            None,
        )
        if not found_company:
            raise InvalidConfigurationError(
                f"export_company_id={self.export_company_id} was not found in Business Central companies"
            )

    def get_reference_data(self) -> ReferenceData:
        self.logger.info("Getting reference data...")

        reference_data: ReferenceData = ReferenceData()
        _, _, companies = self.dynamics_client.get_companies()
        reference_data["companies"] = companies

        self.logger.info("Done getting reference data...")
        return reference_data

    def validate_dimensions_mapping(self, dimensions_mapping: dict):
        # for every company check if the dimension exists
        for company in self.reference_data["companies"]:
            self.logger.info(
                f"Validating field -> dimension mapping for companyId={company['id']}"
            )
            for dimension_name in dimensions_mapping.values():
                found_dimension = next(
                    (
                        dimension
                        for dimension in company["dimensions"]
                        if dimension["code"] == dimension_name
                    ),
                    None,
                )

                if not found_dimension:
                    raise DimensionDefinitionNotFound(
                        f"Could not find dimension={dimension_name} for companyId={company['id']}"
                    )

    def get_tenant_config(self):
        snapshot_directory = self.config.get("snapshot_dir", None)
        tenant_config = {}

        if snapshot_directory:
            config_path = os.path.join(snapshot_directory, "tenant-config.json")
            if not os.path.exists(config_path):
                self.logger.info(
                    f"tenant-config.json does not exist in the snapshot directory={snapshot_directory}"
                )
            else:
                with open(config_path) as f:
                    tenant_config = json.load(f)

        if "dynamics-bc" not in tenant_config:
            tenant_config["dynamics-bc"] = {}

        return tenant_config

    def load_fields_and_dimensions_mapping_config(self):
        tenant_config = self.get_tenant_config()
        dynamics_config = tenant_config.get("dynamics-bc")

        if dynamics_config == None:
            raise InvalidConfigurationError(
                "dynamics-bc is not provided in the tenant-config.json"
            )

        dimensions_mapping = dynamics_config.get("dimension_mappings", {})
        self.validate_dimensions_mapping(dimensions_mapping)

        return dimensions_mapping

    config_jsonschema = th.PropertiesList(
        th.Property("client_id", th.StringType, required=True),
        th.Property("client_secret", th.StringType, required=True),
        th.Property("redirect_uri", th.StringType, required=True),
        th.Property("refresh_token", th.StringType, required=True),
        th.Property("export_company_id", th.StringType, required=True),
    ).to_dict()


if __name__ == "__main__":
    TargetDynamicsV2.cli()
