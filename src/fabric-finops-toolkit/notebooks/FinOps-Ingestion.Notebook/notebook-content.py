# Fabric notebook source

# METADATA ********************
# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "language_info": {
# META     "name": "python"
# META   }
# META }

# CELL ********************

import csv
import gzip
import hashlib
import io
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO

import requests
from azure.kusto.data import KustoConnectionStringBuilder
from azure.kusto.ingest import DataFormat, IngestionProperties, ManagedStreamingIngestClient
from notebookutils import mssparkutils

CONFIG = json.loads("""__CONFIG_JSON__""")
DATABASE_NAME = CONFIG["eventhouse"]["databaseName"]
QUERY_URI = "__QUERY_URI__"


def get_token(resource: str) -> str:
    return mssparkutils.credentials.getToken(resource)


def request(method: str, url: str, *, resource: str, body=None, headers=None):
    request_headers = {"Authorization": f"Bearer {get_token(resource)}"}
    request_headers.update(headers or {})
    response = requests.request(method, url, json=body, headers=request_headers, timeout=120)
    response.raise_for_status()
    return response


def normalize_cost(record: dict, scope: str) -> dict:
    def value(*names, default=None):
        for name in names:
            if name in record and record[name] is not None:
                return record[name]
        return default

    tags = value("Tags", "tags", default={})
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Cost record contains invalid JSON tags: {tags}") from exc

    normalized = {
        "IngestionTime": datetime.now(timezone.utc).isoformat(),
        "SourceScope": scope,
        "BillingAccountId": value("BillingAccountId", "billingAccountId", default=""),
        "BillingAccountName": value("BillingAccountName", "billingAccountName", default=""),
        "BillingCurrency": value("BillingCurrency", "billingCurrency", "Currency", default=""),
        "BillingPeriodStart": value("BillingPeriodStart", "billingPeriodStartDate"),
        "BillingPeriodEnd": value("BillingPeriodEnd", "billingPeriodEndDate"),
        "ChargePeriodStart": value("ChargePeriodStart", "date"),
        "ChargePeriodEnd": value("ChargePeriodEnd", "date"),
        "ChargeCategory": value("ChargeCategory", "chargeType", default=""),
        "ChargeDescription": value("ChargeDescription", "meterName", default=""),
        "ChargeFrequency": value("ChargeFrequency", "frequency", default=""),
        "ChargeSubcategory": value("ChargeSubcategory", "meterSubCategory", default=""),
        "ConsumedQuantity": value("ConsumedQuantity", "usageQuantity", "Quantity", default=0.0),
        "ConsumedUnit": value("ConsumedUnit", "unitOfMeasure", "UnitOfMeasure", default=""),
        "ContractedCost": value("ContractedCost", default=0.0),
        "EffectiveCost": value("EffectiveCost", "Cost", "PreTaxCost", default=0.0),
        "ListCost": value("ListCost", default=0.0),
        "BilledCost": value("BilledCost", "Cost", "PreTaxCost", default=0.0),
        "PricingQuantity": value("PricingQuantity", "Quantity", default=0.0),
        "PricingUnit": value("PricingUnit", "UnitOfMeasure", default=""),
        "ProviderName": value("ProviderName", default="Microsoft"),
        "PublisherName": value("PublisherName", "publisherName", default="Microsoft"),
        "RegionName": value("RegionName", "resourceLocation", "ResourceLocation", default=""),
        "ResourceId": value("ResourceId", "resourceId", "InstanceId", default=""),
        "ResourceName": value("ResourceName", "resourceName", default=""),
        "ResourceType": value("ResourceType", "resourceType", default=""),
        "ServiceCategory": value("ServiceCategory", "serviceFamily", "ServiceFamily", default=""),
        "ServiceName": value("ServiceName", "serviceName", "ConsumedService", default=""),
        "SkuId": value("SkuId", "skuId", default=""),
        "SkuPriceId": value("SkuPriceId", default=""),
        "SubAccountId": value("SubAccountId", "subscriptionId", "SubscriptionId", default=""),
        "SubAccountName": value("SubAccountName", "subscriptionName", "SubscriptionName", default=""),
        "Tags": tags,
        "RawRecord": record,
    }
    identity = json.dumps({"scope": scope, "record": record}, sort_keys=True, default=str, separators=(",", ":"))
    normalized["RecordId"] = hashlib.sha256(identity.encode()).hexdigest()
    return normalized


def query_costs(scope: str) -> list[dict]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(CONFIG["costManagement"]["lookbackDays"]))
    url = f"https://management.azure.com{scope}/providers/Microsoft.CostManagement/generateCostDetailsReport?api-version=2025-03-01"
    body = {
        "metric": "ActualCost",
        "timePeriod": {"start": start.isoformat(), "end": end.isoformat()},
    }
    response = request("POST", url, resource="https://management.azure.com/", body=body)
    poll_url = response.headers.get("Location")
    if not poll_url:
        raise RuntimeError(f"Cost Details API did not return a polling URL for {scope}.")
    for _ in range(120):
        poll = request("GET", poll_url, resource="https://management.azure.com/")
        result = poll.json()
        blob_link = result.get("blobLink") or result.get("properties", {}).get("blobLink")
        if blob_link:
            download = requests.get(blob_link, timeout=300)
            download.raise_for_status()
            content = download.content
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)
            return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        status = (result.get("status") or result.get("properties", {}).get("status") or "").lower()
        if status in {"failed", "canceled", "cancelled"}:
            raise RuntimeError(f"Cost Details report failed for {scope}: {result}")
        time.sleep(int(poll.headers.get("Retry-After", "10")))
    raise TimeoutError(f"Cost Details report did not complete for {scope}.")


def ingest(table: str, rows: list[dict]) -> None:
    if not rows:
        return
    token = get_token(QUERY_URI)
    connection = KustoConnectionStringBuilder.with_aad_user_token_authentication(QUERY_URI, token)
    client = ManagedStreamingIngestClient(connection)
    payload = "\n".join(json.dumps(row, default=str, separators=(",", ":")) for row in rows).encode()
    properties = IngestionProperties(database=DATABASE_NAME, table=table, data_format=DataFormat.MULTIJSON)
    client.ingest_from_stream(BytesIO(payload), ingestion_properties=properties)


run_id = str(uuid.uuid4())
started = datetime.now(timezone.utc)
all_rows = []
try:
    for scope in CONFIG["costManagement"]["scopes"]:
        all_rows.extend(normalize_cost(row, scope) for row in query_costs(scope))
    ingest("FinOpsCosts", all_rows)
    status, error = "Succeeded", ""
except Exception as exc:
    status, error = "Failed", str(exc)
    raise
finally:
    completed = datetime.now(timezone.utc)
    ingest("IngestionRuns", [{
        "RunId": run_id,
        "PipelineName": "FinOps-Ingestion",
        "StartedAt": started.isoformat(),
        "CompletedAt": completed.isoformat(),
        "Status": status,
        "RowsRead": len(all_rows),
        "RowsWritten": len(all_rows) if status == "Succeeded" else 0,
        "Source": "Azure Cost Management Query API",
        "ErrorCode": "",
        "ErrorMessage": error,
        "Details": {"scopes": CONFIG["costManagement"]["scopes"]},
    }])
