#requires -Version 7.2

[CmdletBinding()]
param(
    [Parameter()]
    [string] $Configuration = (Join-Path $PSScriptRoot "config.json"),

    [Parameter()]
    [switch] $RunPipelines
)

$ErrorActionPreference = "Stop"
$script:FabricApi = "https://api.fabric.microsoft.com/v1"

function Get-FabricToken {
    $azAccounts = Get-Module -ListAvailable -Name Az.Accounts | Select-Object -First 1
    if ($azAccounts) {
        Import-Module Az.Accounts
        $token = Get-AzAccessToken -ResourceUrl "https://api.fabric.microsoft.com"
        if ($token.Token -is [securestring]) {
            return [System.Net.NetworkCredential]::new("", $token.Token).Password
        }
        return $token.Token
    }

    if (Get-Command az -ErrorAction SilentlyContinue) {
        return (& az account get-access-token --resource "https://api.fabric.microsoft.com" --query accessToken -o tsv)
    }

    throw "Authentication requires Az.Accounts (Connect-AzAccount) or Azure CLI (az login)."
}

function Invoke-FabricRequest {
    param(
        [Parameter(Mandatory)]
        [ValidateSet("GET", "POST", "DELETE")]
        [string] $Method,

        [Parameter(Mandatory)]
        [string] $Path,

        [Parameter()]
        [object] $Body
    )

    $headers = @{
        Authorization = "Bearer $script:Token"
        "Content-Type" = "application/json"
    }
    $uri = if ($Path.StartsWith("https://")) { $Path } else { "$script:FabricApi/$($Path.TrimStart('/'))" }
    $parameters = @{
        Uri = $uri
        Method = $Method
        Headers = $headers
        SkipHttpErrorCheck = $true
    }
    if ($null -ne $Body) {
        $parameters.Body = $Body | ConvertTo-Json -Depth 100 -Compress
    }

    $response = Invoke-WebRequest @parameters
    if ($response.StatusCode -ge 400) {
        throw "Fabric API $Method $uri failed with HTTP $($response.StatusCode): $($response.Content)"
    }

    if ($response.StatusCode -eq 202 -and $response.Headers.Location) {
        $operationUri = [string]$response.Headers.Location
        for ($attempt = 0; $attempt -lt 120; $attempt++) {
            Start-Sleep -Seconds ([Math]::Max(2, [int]($response.Headers["Retry-After"] ?? 2)))
            $operation = Invoke-WebRequest -Uri $operationUri -Method GET -Headers $headers -SkipHttpErrorCheck
            if ($operation.StatusCode -ge 400) {
                throw "Fabric long-running operation failed with HTTP $($operation.StatusCode): $($operation.Content)"
            }
            $operationBody = if ($operation.Content) { $operation.Content | ConvertFrom-Json } else { $null }
            if ($operationBody.status -eq "Succeeded") {
                if ($operation.Headers.Location) {
                    return Invoke-FabricRequest -Method GET -Path ([string]$operation.Headers.Location)
                }
                return $operationBody
            }
            if ($operationBody.status -in @("Failed", "Cancelled", "Canceled")) {
                throw "Fabric long-running operation ended with status $($operationBody.status): $($operation.Content)"
            }
        }
        throw "Fabric long-running operation timed out: $operationUri"
    }

    if (-not $response.Content) {
        return $null
    }
    return $response.Content | ConvertFrom-Json
}

function ConvertTo-DefinitionPart {
    param(
        [Parameter(Mandatory)]
        [string] $Path,

        [Parameter(Mandatory)]
        [string] $Content
    )

    return @{
        path = $Path.Replace("\", "/")
        payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Content))
        payloadType = "InlineBase64"
    }
}

function Get-FolderDefinition {
    param(
        [Parameter(Mandatory)]
        [string] $Folder,

        [Parameter()]
        [hashtable] $Replacements = @{}
    )

    $root = (Resolve-Path $Folder).Path
    $parts = foreach ($file in Get-ChildItem -Path $root -File -Recurse) {
        if ($file.FullName -match "[\\/]\.pbi[\\/]") {
            continue
        }
        $relativePath = $file.FullName.Substring($root.Length + 1).Replace("\", "/")
        $content = Get-Content -Raw -Path $file.FullName
        foreach ($entry in $Replacements.GetEnumerator()) {
            $content = $content.Replace([string]$entry.Key, [string]$entry.Value)
        }
        ConvertTo-DefinitionPart -Path $relativePath -Content $content
    }
    return @{ parts = @($parts) }
}

function Get-FabricItem {
    param(
        [Parameter(Mandatory)]
        [string] $WorkspaceId,

        [Parameter(Mandatory)]
        [string] $Type,

        [Parameter(Mandatory)]
        [string] $DisplayName
    )

    $result = Invoke-FabricRequest -Method GET -Path "workspaces/$WorkspaceId/items?type=$Type"
    return $result.value | Where-Object displayName -eq $DisplayName | Select-Object -First 1
}

function Set-FabricItem {
    param(
        [Parameter(Mandatory)]
        [string] $WorkspaceId,

        [Parameter(Mandatory)]
        [string] $Collection,

        [Parameter(Mandatory)]
        [string] $Type,

        [Parameter(Mandatory)]
        [string] $DisplayName,

        [Parameter(Mandatory)]
        [hashtable] $Definition
    )

    $existing = Get-FabricItem -WorkspaceId $WorkspaceId -Type $Type -DisplayName $DisplayName
    if ($existing) {
        Write-Host "Updating $Type '$DisplayName'..."
        Invoke-FabricRequest -Method POST -Path "workspaces/$WorkspaceId/$Collection/$($existing.id)/updateDefinition" -Body @{
            definition = $Definition
        } | Out-Null
        return $existing
    }

    Write-Host "Creating $Type '$DisplayName'..."
    $created = Invoke-FabricRequest -Method POST -Path "workspaces/$WorkspaceId/$Collection" -Body @{
        displayName = $DisplayName
        definition = $Definition
    }
    if ($created.id) {
        return $created
    }
    $resolved = Get-FabricItem -WorkspaceId $WorkspaceId -Type $Type -DisplayName $DisplayName
    if (-not $resolved) {
        throw "Fabric created $Type '$DisplayName', but the item could not be resolved."
    }
    return $resolved
}

if (-not (Test-Path $Configuration -PathType Leaf)) {
    throw "Configuration file '$Configuration' was not found. Copy config.example.json to config.json and update it."
}

$config = Get-Content -Raw -Path $Configuration | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace($config.workspace.displayName)) {
    throw "workspace.displayName is required."
}
if ($config.workspace.capacityId -eq "00000000-0000-0000-0000-000000000000") {
    throw "Set workspace.capacityId to a Fabric-enabled capacity ID."
}

$script:Token = Get-FabricToken
$workspaceList = Invoke-FabricRequest -Method GET -Path "workspaces"
$workspace = $workspaceList.value | Where-Object displayName -eq $config.workspace.displayName | Select-Object -First 1
if (-not $workspace) {
    Write-Host "Creating workspace '$($config.workspace.displayName)'..."
    $workspace = Invoke-FabricRequest -Method POST -Path "workspaces" -Body @{
        displayName = $config.workspace.displayName
        description = "Fabric-native FinOps and AI spend analytics."
    }
}

$workspaceId = [string]$workspace.id
Invoke-FabricRequest -Method POST -Path "workspaces/$workspaceId/assignToCapacity" -Body @{
    capacityId = $config.workspace.capacityId
} | Out-Null

$eventhouse = Get-FabricItem -WorkspaceId $workspaceId -Type "Eventhouse" -DisplayName $config.eventhouse.displayName
if (-not $eventhouse) {
    Write-Host "Creating Eventhouse '$($config.eventhouse.displayName)'..."
    $eventhouse = Invoke-FabricRequest -Method POST -Path "workspaces/$workspaceId/eventhouses" -Body @{
        displayName = $config.eventhouse.displayName
    }
}
$eventhouse = Invoke-FabricRequest -Method GET -Path "workspaces/$workspaceId/eventhouses/$($eventhouse.id)"
$queryUri = [string]$eventhouse.properties.queryServiceUri
if ([string]::IsNullOrWhiteSpace($queryUri)) {
    throw "The Eventhouse queryServiceUri was not returned."
}

$properties = Get-Content -Raw -Path (Join-Path $PSScriptRoot "eventhouse\DatabaseProperties.json")
$properties = $properties.Replace("__EVENTHOUSE_ID__", [string]$eventhouse.id)
$properties = $properties.Replace("__CACHE_PERIOD__", [string]$config.eventhouse.cachePeriod)
$properties = $properties.Replace("__STORAGE_PERIOD__", [string]$config.eventhouse.storagePeriod)
$schema = Get-Content -Raw -Path (Join-Path $PSScriptRoot "eventhouse\DatabaseSchema.kql")
$databaseDefinition = @{
    parts = @(
        (ConvertTo-DefinitionPart -Path "DatabaseProperties.json" -Content $properties),
        (ConvertTo-DefinitionPart -Path "DatabaseSchema.kql" -Content $schema)
    )
}
$database = Set-FabricItem -WorkspaceId $workspaceId -Collection "kqlDatabases" -Type "KQLDatabase" -DisplayName $config.eventhouse.databaseName -Definition $databaseDefinition

$configJson = $config | ConvertTo-Json -Depth 30 -Compress
$notebookReplacements = @{
    "__CONFIG_JSON__" = $configJson.Replace("\", "\\").Replace('"""', '\"\"\"')
    "__QUERY_URI__" = $queryUri
}
$finOpsNotebook = Set-FabricItem -WorkspaceId $workspaceId -Collection "notebooks" -Type "Notebook" -DisplayName "FinOps Ingestion" -Definition (
    Get-FolderDefinition -Folder (Join-Path $PSScriptRoot "notebooks\FinOps-Ingestion.Notebook") -Replacements $notebookReplacements
)
$aiNotebook = Set-FabricItem -WorkspaceId $workspaceId -Collection "notebooks" -Type "Notebook" -DisplayName "AI Reporting Ingestion" -Definition (
    Get-FolderDefinition -Folder (Join-Path $PSScriptRoot "notebooks\AI-Reporting.Notebook") -Replacements $notebookReplacements
)

$modelReplacements = @{
    "__QUERY_URI__" = $queryUri
    "__DATABASE_NAME__" = [string]$config.eventhouse.databaseName
}
$finOpsModel = Set-FabricItem -WorkspaceId $workspaceId -Collection "semanticModels" -Type "SemanticModel" -DisplayName "FinOps Model" -Definition (
    Get-FolderDefinition -Folder (Join-Path $PSScriptRoot "power-bi\FinOps.SemanticModel") -Replacements $modelReplacements
)
$aiModel = Set-FabricItem -WorkspaceId $workspaceId -Collection "semanticModels" -Type "SemanticModel" -DisplayName "AI Spend Model" -Definition (
    Get-FolderDefinition -Folder (Join-Path $PSScriptRoot "power-bi\AI-Spend.SemanticModel") -Replacements $modelReplacements
)

$finOpsReport = Set-FabricItem -WorkspaceId $workspaceId -Collection "reports" -Type "Report" -DisplayName "FinOps Summary Report" -Definition (
    Get-FolderDefinition -Folder (Join-Path $PSScriptRoot "power-bi\FinOps.Report") -Replacements @{
        "__SEMANTIC_MODEL_ID__" = [string]$finOpsModel.id
    }
)
$aiReport = Set-FabricItem -WorkspaceId $workspaceId -Collection "reports" -Type "Report" -DisplayName "AI Spend Report" -Definition (
    Get-FolderDefinition -Folder (Join-Path $PSScriptRoot "power-bi\AI-Spend.Report") -Replacements @{
        "__SEMANTIC_MODEL_ID__" = [string]$aiModel.id
    }
)

$pipelineReplacements = @{
    "__WORKSPACE_ID__" = $workspaceId
    "__FINOPS_NOTEBOOK_ID__" = [string]$finOpsNotebook.id
    "__AI_NOTEBOOK_ID__" = [string]$aiNotebook.id
}
$finOpsPipelineContent = Get-Content -Raw -Path (Join-Path $PSScriptRoot "pipelines\FinOps-Ingestion.pipeline-content.json")
$aiPipelineContent = Get-Content -Raw -Path (Join-Path $PSScriptRoot "pipelines\AI-Reporting.pipeline-content.json")
foreach ($entry in $pipelineReplacements.GetEnumerator()) {
    $finOpsPipelineContent = $finOpsPipelineContent.Replace([string]$entry.Key, [string]$entry.Value)
    $aiPipelineContent = $aiPipelineContent.Replace([string]$entry.Key, [string]$entry.Value)
}
$finOpsPipeline = Set-FabricItem -WorkspaceId $workspaceId -Collection "dataPipelines" -Type "DataPipeline" -DisplayName "FinOps Ingestion" -Definition @{
    parts = @((ConvertTo-DefinitionPart -Path "pipeline-content.json" -Content $finOpsPipelineContent))
}
$aiPipeline = Set-FabricItem -WorkspaceId $workspaceId -Collection "dataPipelines" -Type "DataPipeline" -DisplayName "AI Reporting" -Definition @{
    parts = @((ConvertTo-DefinitionPart -Path "pipeline-content.json" -Content $aiPipelineContent))
}

if ($RunPipelines) {
    foreach ($pipeline in @($finOpsPipeline, $aiPipeline)) {
        Write-Host "Starting pipeline '$($pipeline.displayName)'..."
        Invoke-FabricRequest -Method POST -Path "workspaces/$workspaceId/items/$($pipeline.id)/jobs/instances?jobType=Pipeline" -Body @{} | Out-Null
    }
}

$deployment = [ordered]@{
    workspaceId = $workspaceId
    eventhouseId = [string]$eventhouse.id
    queryUri = $queryUri
    databaseId = [string]$database.id
    finOpsNotebookId = [string]$finOpsNotebook.id
    aiNotebookId = [string]$aiNotebook.id
    finOpsSemanticModelId = [string]$finOpsModel.id
    aiSemanticModelId = [string]$aiModel.id
    finOpsReportId = [string]$finOpsReport.id
    aiReportId = [string]$aiReport.id
    finOpsPipelineId = [string]$finOpsPipeline.id
    aiPipelineId = [string]$aiPipeline.id
}
$deployment | ConvertTo-Json | Set-Content -Path (Join-Path $PSScriptRoot "deployment-output.json") -Encoding utf8
$deployment
