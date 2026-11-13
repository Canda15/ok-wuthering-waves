param(
    [switch]$PrepareActionOnly,
    [string]$ActionDirectory = 'build/nono-launcher-action',
    [string]$OutputDirectory = 'build/launcher-dist'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$projectPath = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$buildPath = [IO.Path]::GetFullPath((Join-Path $projectPath 'build'))
$pythonPath = if (Test-Path -LiteralPath (Join-Path $projectPath '.venv/Scripts/python.exe')) {
    Join-Path $projectPath '.venv/Scripts/python.exe'
} else {
    (Get-Command python -CommandType Application | Select-Object -First 1).Source
}
$actionPath = [IO.Path]::GetFullPath((Join-Path $projectPath $ActionDirectory))
$outputPath = [IO.Path]::GetFullPath((Join-Path $projectPath $OutputDirectory))
foreach ($targetPath in @($actionPath, $outputPath)) {
    if (-not $targetPath.StartsWith($buildPath + [IO.Path]::DirectorySeparatorChar,
                                    [StringComparison]::OrdinalIgnoreCase)) {
        throw '启动器构建目录必须位于本仓库 build 内。'
    }
}
if (Test-Path -LiteralPath $actionPath) {
    throw '固定 Action 必须检出到新目录，禁止复用现成 EXE 或旧适配源码。'
}
New-Item -ItemType Directory -Path $buildPath -Force | Out-Null

Push-Location -LiteralPath $projectPath
try {
    & $pythonPath -B launcher/apply.py --check
    if ($LASTEXITCODE -ne 0) { throw '启动器适配来源检查失败。' }
    & git -c core.autocrlf=false clone --no-checkout 'https://github.com/ok-oldking/pyappify-action.git' $actionPath
    if ($LASTEXITCODE -ne 0) { throw '固定 Action 下载失败。' }
    & git -C $actionPath -c core.autocrlf=false checkout --detach '6dcb02954bb5fab5b83e95f1af243078213ec1ab'
    if ($LASTEXITCODE -ne 0) { throw '固定 Action 检出失败。' }
    & $pythonPath -B launcher/apply.py --action-root $actionPath
    if ($LASTEXITCODE -ne 0) { throw '固定 Action 适配失败。' }
    Push-Location -LiteralPath $actionPath
    try {
        & npm.cmd ci --ignore-scripts
        if ($LASTEXITCODE -ne 0) { throw '固定 Action 依赖安装失败。' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Action 的实际 dist 入口重建失败。' }
        & node --check (Join-Path $actionPath 'dist/index.js')
        if ($LASTEXITCODE -ne 0) { throw '重建 Action 入口语法检查失败。' }
    } finally { Pop-Location }
    if (-not (Test-Path -LiteralPath (Join-Path $actionPath 'dist/index.js'))) {
        throw '缺少重建后的实际 Action 入口。'
    }
    if ($PrepareActionOnly) {
        if ($env:GITHUB_OUTPUT) {
            "action-path=$actionPath" >> $env:GITHUB_OUTPUT
            "python-path=$pythonPath" >> $env:GITHUB_OUTPUT
        }
        Write-Host '已准备固定 Action 与重建的 ncc 入口。'
        return
    }

    # Action 在自己的构建工作目录运行，官方清理步骤不能触及用户源码或原有产物。
    $stagePath = Join-Path $buildPath ("launcher-build-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stagePath | Out-Null
    $compilerTemp = Join-Path $stagePath 'temp'
    New-Item -ItemType Directory -Path $compilerTemp | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectPath 'pyappify.yml') -Destination $stagePath
    Copy-Item -LiteralPath (Join-Path $projectPath 'icons') -Destination $stagePath -Recurse
    $inputValues = @{
        INPUT_VERSION = 'v1.2.7'
        INPUT_NONO_SOURCE_HOOK = (Join-Path $projectPath 'launcher/apply.py')
        INPUT_USE_RELEASE = ''
        INPUT_BUILD_EXE_ONLY = 'true'
        INPUT_BUILD_SETUP = 'false'
        INPUT_ADD_VERSION_NAME = 'false'
        INPUT_PACKAGE_ZIP = 'false'
        INPUT_PACKAGE_ZIP_FORMAT = 'zip'
        INPUT_PACKAGE_ZIP_UPDATER = 'false'
        NONO_BUILD_PYTHON = $pythonPath
        RUNNER_TEMP = $stagePath
        TMP = $compilerTemp
        TEMP = $compilerTemp
    }
    $previousValues = @{}
    foreach ($entry in $inputValues.GetEnumerator()) {
        $previousValues[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, 'Process')
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }
    Push-Location -LiteralPath $stagePath
    try {
        & node (Join-Path $actionPath 'dist/index.js')
        if ($LASTEXITCODE -ne 0) { throw '新版 NoNo 启动器编译失败。' }
    } finally {
        Pop-Location
        foreach ($entry in $previousValues.GetEnumerator()) {
            [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
        }
    }
    $executablePath = Join-Path $stagePath 'p/src-tauri/target/release/NoNo.exe'
    if (-not (Test-Path -LiteralPath $executablePath)) { throw '固定 Action 未产出 NoNo.exe。' }
    New-Item -ItemType Directory -Path $outputPath -Force | Out-Null
    Copy-Item -LiteralPath $executablePath -Destination (Join-Path $outputPath 'NoNo.exe')
    $verifyArguments = @('-B', 'launcher/verify.py', '--exe', (Join-Path $outputPath 'NoNo.exe'),
        '--output', (Join-Path $outputPath 'launcher-build.json'))
    & $pythonPath @verifyArguments
    if ($LASTEXITCODE -ne 0) { throw '新版启动器工件核验失败。' }
    Write-Host '已生成并核验 NoNo 1.2.7 启动器工件，尚未发布。'
} finally { Pop-Location }
