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
import hashlib
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import quote

import requests
from azure.kusto.data import KustoConnectionStringBuilder
from azure.kusto.ingest import DataFormat, IngestionProperties, ManagedStreamingIngestClient
from notebookutils import mssparkutils

CONFIG = json.loads("""__CONFIG_JSON__""")
DATABASE_NAME = CONFIG["eventhouse"]["databaseName"]
QUERY_URI = "__QUERY_URI__"


def get_token(resource: str) -> str:
    return mssparkutils.credentials.getToken(resource)


def get_secret(name: str) -> str:
    vault = CONFIG.get("keyVaultUri", "")
    if not vault or not name:
        return ""
    return mssparkutils.credentials.getSecret(vault, name)


def get(url: str, *, token: str, accept="application/json"):
    response = requests.get(url, headers={"Authorization": f"Bearer {token}", "Accept": accept}, timeout=120)
    response.raise_for_status()
    return response


def base_row(provider: str, product: str, raw: dict) -> dict:
    tags = raw.get("tags", {})
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError as exc:
            raise ValueError(f"AI usage record contains invalid JSON tags: {tags}") from exc

    normalized = {
        "IngestionTime": datetime.now(timezone.utc).isoformat(),
        "UsageDate": raw.get("reportRefreshDate") or raw.get("lastActivityDate") or raw.get("date"),
        "Provider": provider,
        "Product": product,
        "TenantId": raw.get("tenantId", ""),
        "SubscriptionId": raw.get("subscriptionId", ""),
        "ResourceId": raw.get("resourceId", ""),
        "ResourceName": raw.get("resourceName", ""),
        "Organization": raw.get("organization", ""),
        "Project": raw.get("project", ""),
        "Application": raw.get("application", ""),
        "Environment": raw.get("environment", ""),
        "UserId": raw.get("userPrincipalName") or raw.get("userId", ""),
        "AgentId": raw.get("agentId", ""),
        "AgentName": raw.get("agentName", ""),
        "Model": raw.get("model", ""),
        "ModelVersion": raw.get("modelVersion", ""),
        "Region": raw.get("region", ""),
        "UsageType": raw.get("usageType", ""),
        "InputTokens": int(raw.get("inputTokens") or 0),
        "OutputTokens": int(raw.get("outputTokens") or 0),
        "CachedTokens": int(raw.get("cachedTokens") or 0),
        "TotalTokens": int(raw.get("totalTokens") or 0),
        "Requests": int(raw.get("requests") or raw.get("promptsSubmitted") or 0),
        "ActiveUsers": int(raw.get("activeUsers") or 0),
        "Seats": int(raw.get("seats") or raw.get("totalSeats") or 0),
        "CopilotCredits": float(raw.get("copilotCredits") or raw.get("creditsUsed") or 0),
        "Quantity": float(raw.get("quantity") or 0),
        "Unit": raw.get("unit", ""),
        "Currency": raw.get("currency", ""),
        "BilledCost": float(raw.get("billedCost") or raw.get("cost") or 0),
        "EffectiveCost": float(raw.get("effectiveCost") or raw.get("cost") or 0),
        "CostSource": raw.get("costSource", "Provider usage API"),
        "Tags": tags,
        "RawRecord": raw,
    }
    identity = json.dumps({"provider": provider, "product": product, "record": raw}, sort_keys=True, default=str, separators=(",", ":"))
    normalized["UsageId"] = hashlib.sha256(identity.encode()).hexdigest()
    return normalized


def microsoft_365_copilot() -> list[dict]:
    endpoint = CONFIG["ai"].get("microsoftGraphEndpoint", "")
    if not endpoint:
        return []
    response = get(endpoint, token=get_token("https://graph.microsoft.com/"), accept="text/csv")
    records = list(csv.DictReader(io.StringIO(response.text)))
    return [base_row("Microsoft 365", "Microsoft 365 Copilot", record) for record in records]


def azure_ai_monitor() -> list[dict]:
    subscriptions = sorted({
        scope.split("/")[2]
        for scope in CONFIG["costManagement"]["scopes"]
        if scope.lower().startswith("/subscriptions/") and len(scope.split("/")) > 2
    })
    if not subscriptions:
        return []
    token = get_token("https://management.azure.com/")
    graph_response = requests.post(
        "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "subscriptions": subscriptions,
            "query": "resources | where type in~ ('microsoft.cognitiveservices/accounts', 'microsoft.machinelearningservices/workspaces') | project id, name, type, location, subscriptionId, tags",
        },
        timeout=120,
    )
    graph_response.raise_for_status()
    rows = []
    for resource in graph_response.json().get("data", []):
        definitions_url = (
            f"https://management.azure.com{resource['id']}/providers/microsoft.insights/"
            "metricDefinitions?api-version=2018-01-01"
        )
        definitions = get(definitions_url, token=token).json().get("value", [])
        metric_names = [
            definition["name"]["value"]
            for definition in definitions
            if any(term in definition["name"]["value"].lower() for term in ("token", "request", "transaction"))
        ][:20]
        if not metric_names:
            continue
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=int(CONFIG["ai"]["lookbackDays"]))
        timespan = quote(f"{start.isoformat()}/{end.isoformat()}")
        metrics_url = (
            f"https://management.azure.com{resource['id']}/providers/microsoft.insights/metrics"
            f"?api-version=2018-01-01&metricnames={quote(','.join(metric_names))}"
            f"&timespan={timespan}&interval=P1D&aggregation=Total"
        )
        metrics = get(metrics_url, token=token).json().get("value", [])
        for metric in metrics:
            metric_name = metric.get("name", {}).get("value", "")
            for series in metric.get("timeseries", []):
                metadata = {
                    item.get("name", {}).get("value", ""): item.get("value", "")
                    for item in series.get("metadatavalues", [])
                }
                for point in series.get("data", []):
                    total = point.get("total")
                    if total is None:
                        continue
                    lowered = metric_name.lower()
                    raw = {
                        "date": point.get("timeStamp"),
                        "subscriptionId": resource.get("subscriptionId", ""),
                        "resourceId": resource["id"],
                        "resourceName": resource["name"],
                        "region": resource.get("location", ""),
                        "model": metadata.get("ModelDeploymentName") or metadata.get("ModelName") or "",
                        "modelVersion": metadata.get("ModelVersion", ""),
                        "usageType": metric_name,
                        "inputTokens": total if any(term in lowered for term in ("prompt", "input")) else 0,
                        "outputTokens": total if any(term in lowered for term in ("generated", "output")) else 0,
                        "totalTokens": total if "token" in lowered else 0,
                        "requests": total if any(term in lowered for term in ("request", "transaction")) else 0,
                        "quantity": total,
                        "unit": metric.get("unit", "Count"),
                        "tags": resource.get("tags") or {},
                    }
                    product = "Microsoft Foundry" if "machinelearningservices" in resource["type"].lower() else "Azure OpenAI"
                    rows.append(base_row("Microsoft Azure", product, raw))
    return rows


def github_copilot() -> list[dict]:
    token = get_secret(CONFIG["ai"].get("githubTokenSecretName", ""))
    if not token:
        return []
    rows = []
    for organization in CONFIG["ai"].get("githubOrganizations", []):
        url = f"https://api.github.com/orgs/{organization}/copilot/billing/seats"
        response = get(url, token=token)
        body = response.json()
        raw = {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "organization": organization,
            "seats": body.get("total_seats", 0),
            "activeUsers": len([seat for seat in body.get("seats", []) if seat.get("last_activity_at")]),
            "usageType": "Assigned seats",
            "quantity": body.get("total_seats", 0),
            "unit": "Seats",
        }
        rows.append(base_row("GitHub", "GitHub Copilot", raw))
    return rows


def copilot_studio() -> list[dict]:
    endpoint = CONFIG["ai"].get("copilotStudioUsageEndpoint", "")
    token = get_secret(CONFIG["ai"].get("copilotStudioTokenSecretName", ""))
    if not endpoint or not token:
        return []
    body = get(endpoint, token=token).json()
    records = body.get("value", body if isinstance(body, list) else [])
    return [base_row("Microsoft Power Platform", "Copilot Studio", record) for record in records]


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
rows = []
try:
    rows.extend(azure_ai_monitor())
    rows.extend(microsoft_365_copilot())
    rows.extend(github_copilot())
    rows.extend(copilot_studio())
    ingest("AIUsage", rows)
    status, error = "Succeeded", ""
except Exception as exc:
    status, error = "Failed", str(exc)
    raise
finally:
    completed = datetime.now(timezone.utc)
    ingest("IngestionRuns", [{
        "RunId": run_id,
        "PipelineName": "AI-Reporting",
        "StartedAt": started.isoformat(),
        "CompletedAt": completed.isoformat(),
        "Status": status,
        "RowsRead": len(rows),
        "RowsWritten": len(rows) if status == "Succeeded" else 0,
        "Source": "Microsoft Graph, GitHub, Copilot Studio; Azure AI cost is joined from FOCUS",
        "ErrorCode": "",
        "ErrorMessage": error,
        "Details": {"providers": sorted({row["Provider"] for row in rows})},
    }])
