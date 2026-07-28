param(
    [int]$MaxVideos = 0,
    [int]$MaxQuestionsPerVideo = 0,
    [double]$Fps = 0.5,
    [double]$Alpha = 0.1,
    [int]$Seed = 17,
    [ValidateSet("fp16", "4bit")][string]$Precision = "fp16",
    [string]$ModelId = "Qwen/Qwen3-VL-2B-Instruct",
    [ValidateSet("local", "dashscope")][string]$Backend = "local",
    [string]$ApiModel = "qwen3-vl-plus",
    [string[]]$VideoId = @(),
    [string]$OutputName = "egolife_benchmark",
    [string]$Image = "sha256:f0ca81b440e252399d9954a45b616ee2540959466aacf3dfc3f856691eee66e8",
    [switch]$Online
)

$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$workspace = (Resolve-Path (Join-Path $project "..")).Path
$deps = Join-Path $workspace "docker_deps"
$cache = Join-Path $workspace "model_cache"
$data = Join-Path $project "data_real\egolife\A1_JAKE_DAY1"
$output = Join-Path $project "outputs\$OutputName"
New-Item -ItemType Directory -Force -Path $output | Out-Null

$runnerArgs = @(
    "run_egolife_benchmark.py",
    "--data-dir", "/data",
    "--output-dir", "/output",
    "--fps", "$Fps",
    "--alpha", "$Alpha",
    "--seed", "$Seed",
    "--precision", "$Precision",
    "--model-id", "$ModelId",
    "--backend", "$Backend",
    "--api-model", "$ApiModel"
)
foreach ($id in $VideoId) { $runnerArgs += @("--video-id", $id) }
if ($MaxVideos -gt 0) { $runnerArgs += @("--max-videos", "$MaxVideos") }
if ($MaxQuestionsPerVideo -gt 0) {
    $runnerArgs += @("--max-questions-per-video", "$MaxQuestionsPerVideo")
}

$dockerArgs = @(
    "run", "--rm", "--gpus", "all", "--shm-size=4g",
    "-v", "${project}:/app:ro",
    "-v", "${data}:/data:ro",
    "-v", "${output}:/output",
    "-v", "${deps}:/deps:ro",
    "-v", "${cache}:/cache",
    "-e", "HF_HOME=/cache",
    "-w", "/app"
)
if (-not $Online) { $dockerArgs += @("-e", "HF_HUB_OFFLINE=1") }
if ($Backend -eq "dashscope") {
    $envFile = Join-Path $project ".env"
    if (-not (Test-Path $envFile)) { throw "DashScope backend requires $envFile" }
    $dockerArgs += @("--env-file", $envFile)
    $dockerArgs += @("--cpus", "2")
}
$argvJson = (@("run_egolife_benchmark.py") + $runnerArgs[1..($runnerArgs.Count - 1)]) |
    ConvertTo-Json -Compress
$argvBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($argvJson))
$python = "import base64,json,runpy,sys;sys.path.append('/deps');" +
    "sys.argv=json.loads(base64.b64decode('$argvBase64'));" +
    "runpy.run_path('run_egolife_benchmark.py',run_name='__main__')"
$dockerArgs += @($Image, "python", "-c", $python)

Write-Host "EgoLife real subset benchmark"
Write-Host "Data:   $data"
Write-Host "Output: $output"
Write-Host "Mode:   $(if ($Online) { 'online' } else { 'offline cached weights' })"
Write-Host "Backend: $Backend $(if ($Backend -eq 'dashscope') { $ApiModel } else { $ModelId })"
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& docker @dockerArgs
$exitCode = $LASTEXITCODE
$ErrorActionPreference = $previousPreference
if ($exitCode -ne 0) { throw "Benchmark failed with exit code $exitCode" }
