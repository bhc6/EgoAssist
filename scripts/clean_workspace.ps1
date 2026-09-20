param(
    [switch]$Apply,
    [ValidateRange(0, 100)][int]$KeepTestRuns = 3,
    [string]$ReportPath
)

$ErrorActionPreference = 'Stop'
$workspaceRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$rootPrefix = $workspaceRoot + [IO.Path]::DirectorySeparatorChar
if (-not (Test-Path -LiteralPath (Join-Path $workspaceRoot 'pyproject.toml'))) {
    throw 'Run this script from its EgoAssist scripts directory.'
}
if ($Apply) {
    $testProcesses = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^(python|pythonw|pytest)(\.exe)?$' -and
        $_.CommandLine -like "*$workspaceRoot*" -and $_.CommandLine -match 'pytest'
    })
    if ($testProcesses.Count) { throw 'A workspace pytest process is active; retry after it exits.' }
}

$candidates = [Collections.Generic.List[string]]::new()
foreach ($name in @('__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache')) {
    $candidates.Add((Join-Path $workspaceRoot $name))
}
foreach ($name in @('egoassist', 'scripts', 'tests')) {
    Get-ChildItem -LiteralPath (Join-Path $workspaceRoot $name) -Directory -Recurse -Force |
        Where-Object Name -eq '__pycache__' | ForEach-Object { $candidates.Add($_.FullName) }
}
$testRoot = Join-Path $workspaceRoot '.test-runs'
if (Test-Path -LiteralPath $testRoot) {
    Get-ChildItem -LiteralPath $testRoot -Directory -Force |
        Where-Object Name -match '^[a-f0-9]{32}$' | Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $KeepTestRuns | ForEach-Object { $candidates.Add($_.FullName) }
}
foreach ($name in @('answer-completion-wheel', 'architecture-review-wheel',
    'architecture-review-wheel-final', 'core-cleanup-wheel', 'interaction-v16-wheel', 'skill-catalog-wheel')) {
    $candidates.Add((Join-Path $workspaceRoot "outputs/$name"))
}
Get-ChildItem -LiteralPath (Join-Path $workspaceRoot 'outputs') -Directory -Filter 'pytest-cache-*' |
    ForEach-Object { $candidates.Add($_.FullName) }

# Resolve and inspect the complete deletion plan before mutating any path.
$plan = @($candidates | Sort-Object -Unique | ForEach-Object {
    $target = [IO.Path]::GetFullPath($_)
    $item = [ordered]@{path=$target; status='planned'; files=0; bytes=0}
    try {
        if (-not $target.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Path is outside the workspace.'
        }
        if (-not (Test-Path -LiteralPath $target)) { return }
        $ancestor = $target
        while ($ancestor -ne $workspaceRoot) {
            if ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Reparse points are excluded.'
            }
            $ancestor = Split-Path $ancestor -Parent
        }
        $relative = $target.Substring($rootPrefix.Length).Replace('\', '/')
        $tracked = @(git -C $workspaceRoot ls-files --cached -- $relative)
        if ($LASTEXITCODE -ne 0 -or $tracked.Count) { throw 'Tracked files are excluded.' }
        $entries = @(Get-ChildItem -LiteralPath $target -Recurse -Force)
        if (@($entries | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) {
            throw 'Nested reparse points are excluded.'
        }
        $files = @($entries | Where-Object { -not $_.PSIsContainer })
        if ($relative -like 'outputs/*wheel*' -and @($files | Where-Object Extension -ne '.whl').Count) {
            throw 'Wheel folder contains other evidence; excluded.'
        }
        $item.files = $files.Count
        $item.bytes = [long](($files | Measure-Object Length -Sum).Sum)
    } catch {
        $item.status = 'skipped'
        $item.reason = $_.Exception.Message
    }
    [pscustomobject]$item
})

if (-not $ReportPath) {
    $ReportPath = Join-Path $workspaceRoot ('outputs/workspace-cleanup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
}
$ReportPath = [IO.Path]::GetFullPath($ReportPath)
if (-not $ReportPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Report must stay inside the workspace.'
}
if (Test-Path -LiteralPath $ReportPath) { throw 'Use a new report path to preserve earlier manifests.' }
foreach ($item in $plan) {
    if ($ReportPath.StartsWith($item.path + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Report cannot be stored inside a deletion target.'
    }
}
New-Item -ItemType Directory -Path (Split-Path $ReportPath -Parent) -Force | Out-Null
$report = [ordered]@{workspace=$workspaceRoot; apply=[bool]$Apply; keepTestRuns=$KeepTestRuns; plan=$plan}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ReportPath -Encoding utf8
if ($Apply) {
    foreach ($item in $plan) {
        if ($item.status -ne 'planned') { continue }
        try {
            Remove-Item -LiteralPath $item.path -Recurse -Force
            $item.status = 'deleted'
        } catch {
            $item.status = 'failed'
            $item | Add-Member -NotePropertyName reason -NotePropertyValue $_.Exception.Message
        }
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ReportPath -Encoding utf8
}
[pscustomobject]@{
    report=$ReportPath; apply=[bool]$Apply
    planned=@($plan | Where-Object status -eq 'planned').Count
    deleted=@($plan | Where-Object status -eq 'deleted').Count
    skipped=@($plan | Where-Object status -eq 'skipped').Count
    failed=@($plan | Where-Object status -eq 'failed').Count
    deletedBytes=($plan | Where-Object status -eq 'deleted' | Measure-Object bytes -Sum).Sum
} | ConvertTo-Json
