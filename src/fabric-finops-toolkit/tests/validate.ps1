#requires -Version 7.2

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$errors = [Collections.Generic.List[string]]::new()

Get-ChildItem -Path $root -Filter *.json -File -Recurse |
    Where-Object FullName -notmatch "[\\/]config\.json$" |
    ForEach-Object {
        try {
            Get-Content -Raw -Path $_.FullName | ConvertFrom-Json | Out-Null
        }
        catch {
            $errors.Add("Invalid JSON: $($_.FullName): $($_.Exception.Message)")
        }
    }

$requiredFiles = @(
    "deploy.ps1",
    "eventhouse\DatabaseSchema.kql",
    "pipelines\FinOps-Ingestion.pipeline-content.json",
    "pipelines\AI-Reporting.pipeline-content.json",
    "notebooks\FinOps-Ingestion.Notebook\notebook-content.py",
    "notebooks\AI-Reporting.Notebook\notebook-content.py",
    "power-bi\FinOps.SemanticModel\model.bim",
    "power-bi\AI-Spend.SemanticModel\model.bim",
    "power-bi\FinOps.Report\definition.pbir",
    "power-bi\AI-Spend.Report\definition.pbir"
)
foreach ($relativePath in $requiredFiles) {
    if (-not (Test-Path (Join-Path $root $relativePath) -PathType Leaf)) {
        $errors.Add("Missing required artifact: $relativePath")
    }
}

$schema = Get-Content -Raw -Path (Join-Path $root "eventhouse\DatabaseSchema.kql")
foreach ($requiredObject in @("FinOpsCosts", "AIUsage", "IngestionRuns", "Costs()", "AISpend()", "AIDailyTrend()", "AIModelEconomics()")) {
    if (-not $schema.Contains($requiredObject)) {
        $errors.Add("Eventhouse schema is missing $requiredObject")
    }
}

$allowedTokens = @(
    "__EVENTHOUSE_ID__",
    "__CACHE_PERIOD__",
    "__STORAGE_PERIOD__",
    "__WORKSPACE_ID__",
    "__FINOPS_NOTEBOOK_ID__",
    "__AI_NOTEBOOK_ID__",
    "__QUERY_URI__",
    "__DATABASE_NAME__",
    "__SEMANTIC_MODEL_ID__",
    "__CONFIG_JSON__"
)
$tokens = Get-ChildItem -Path $root -File -Recurse |
    Where-Object FullName -notmatch "[\\/]tests[\\/]" |
    Select-String -Pattern "__[A-Z0-9_]+__" -AllMatches |
    ForEach-Object { $_.Matches.Value } |
    Sort-Object -Unique
foreach ($token in $tokens) {
    if ($token -notin $allowedTokens) {
        $errors.Add("Unknown deployment token: $token")
    }
}

if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Error $_ }
    exit 1
}

Write-Host "Validated JSON, required artifacts, Eventhouse objects, and deployment tokens."
