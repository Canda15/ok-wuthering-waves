param(
    [switch]$TestsOnly
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonPath = if (Test-Path -LiteralPath (Join-Path $projectPath '.venv\Scripts\python.exe')) {
    Join-Path $projectPath '.venv\Scripts\python.exe'
} elseif (Test-Path -LiteralPath (Join-Path $projectPath '.venv\bin\python')) {
    Join-Path $projectPath '.venv\bin\python'
} else {
    (Get-Command python -CommandType Application | Select-Object -First 1).Source
}

$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTHONIOENCODING = 'UTF-8'
# 测试子进程只验证替身与临时窗口，不继承 CI 的仓库／通知凭据。
foreach ($variable in @(
    'GITHUB_TOKEN', 'GH_TOKEN', 'CNB_TOKEN', 'OK_GH', 'SIGNPATH_API_TOKEN',
    'GEMINI_API_KEY', 'AI_API_KEY', 'MirrorChyanUploadToken',
    'ACTIONS_RUNTIME_TOKEN', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN'
)) {
    Remove-Item -LiteralPath "Env:\$variable" -ErrorAction SilentlyContinue
}

$testFiles = @(
    'tests/TestForegroundInput.py',
    'tests/TestForegroundNotification.py',
    'tests/TestMultiAccountConfig.py',
    'tests/TestMultiAccountDailyTask.py',
    'tests/test_tacet_elimination.py',
    'tests/TestOneTimeCombatRestart.py',
    'tests/TestDomainRecoveryLoop.py',
    'tests/TestFarmEchoTeleport.py',
    'tests/TestSkipDialogConfirm.py',
    'tests/TestBaseCombatTask.py',
    'tests/TestCombatCheck.py',
    'tests/TestAbyssRestart.py',
    'tests/TestChar.py',
    'tests/TestLily.py',
    'tests/TestHiyukiSkin.py',
    'tests/TestSelfhostSmoke.py'
)

# run_test.py 统一隔离配置、截图和日志；每个文件独立进程，避免 Qt 和执行器状态串用。

Push-Location -LiteralPath $projectPath
try {
    # 开发阶段可先验证正在编辑的源码；CI 始终使用默认完整门禁。
    if (-not $TestsOnly) {
        & $pythonPath -X utf8 -B 'scripts/sync_ok.py' --check
        if ($LASTEXITCODE -ne 0) { throw "内联框架与逻辑差异校验失败，退出码 $LASTEXITCODE。" }
    }
    foreach ($testFile in $testFiles) {
        Write-Host "运行前台核心测试：$testFile"
        & $pythonPath -X utf8 -B -u 'run_test.py' $testFile
        if ($LASTEXITCODE -ne 0) { throw "前台核心测试失败：$testFile，退出码 $LASTEXITCODE。" }
    }
} finally {
    Pop-Location
}
Write-Host '前台核心检查通过。'
