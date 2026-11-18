param(
    [switch]$TestsOnly,
    [switch]$Quick
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
    'tests/TestBackgroundInput.py',
    'tests/TestGameLauncher.py',
    'tests/TestForegroundNotification.py',
    'tests/TestMultiAccountConfig.py',
    'tests/TestMultiAccountDailyTask.py',
    'tests/test_tacet_elimination.py',
    'tests/TestOneTimeCombatRestart.py',
    'tests/TestDomainRecoveryLoop.py',
    'tests/TestFarmEchoTeleport.py',
    'tests/TestFarmEchoPickup.py',
    'tests/TestSkipDialogConfirm.py',
    'tests/TestBaseCombatTask.py',
    'tests/TestCombatCheck.py',
    'tests/TestCloudCombatTarget.py',
    'tests/TestCloudDisconnect.py',
    'tests/TestAbyssRestart.py',
    'tests/TestChar.py',
    'tests/TestLily.py',
    'tests/TestHiyukiSkin.py',
    'tests/TestSelfhostSmoke.py'
)

$testGroups = if ($Quick) {
    # 日常推送只保留关键接线和输入保护；同一文件的选择器合并运行以减少进程启动。
    @(
        @{
            Name = 'tests/TestForegroundInput.py'
            Targets = @(
                'tests.TestForegroundInput.TestForegroundInput',
                'tests.TestForegroundInput.TestForegroundLifecycle.test_安全等待回前台刷新代次和新帧但不置前',
                'tests.TestForegroundInput.TestForegroundDeviceAndWindow.test_新旧公开入口保持同一前台类型'
            )
        },
        @{
            Name = 'tests/TestBackgroundInput.py'
            Targets = @(
                'tests.TestBackgroundInput.TestBackgroundInput',
                'tests.TestBackgroundInput.TestBackgroundMouseResetTask'
            )
        },
        @{
            Name = 'tests/TestSelfhostSmoke.py'
            Targets = @(
                'tests.TestSelfhostSmoke.TestSelfhostSmoke.test_default_input_policy_and_background_mouse_reset_registration',
                'tests.TestSelfhostSmoke.TestSelfhostSmoke.test_xmod_versions_sort_as_branded_releases_in_both_python_consumers',
                'tests.TestSelfhostSmoke.TestSelfhostSmoke.test_main_window_still_consumes_custom_tabs',
                'tests.TestSelfhostSmoke.TestSelfhostSmoke.test_registered_tabs_instantiate_against_current_framework',
                'tests.TestSelfhostSmoke.TestUserDataMigration.test_real_main_imports_existing_accounts_and_team_after_migration'
            )
        }
    )
} else {
    foreach ($testFile in $testFiles) {
        @{ Name = $testFile; Targets = @($testFile) }
    }
}
$checkName = if ($Quick) { '快速' } else { '输入与任务核心' }

# run_test.py 统一隔离配置、截图和日志；每个文件独立进程，避免 Qt 和执行器状态串用。
Push-Location -LiteralPath $projectPath
try {
    # 快速与完整检查都核对来源；TestsOnly 仅供尚未登记差异的开发阶段使用。
    if (-not $TestsOnly) {
        & $pythonPath -X utf8 -B 'scripts/sync_ok.py' --check
        if ($LASTEXITCODE -ne 0) { throw "内联框架与逻辑差异校验失败，退出码 $LASTEXITCODE。" }
    }
    foreach ($testGroup in $testGroups) {
        Write-Host "运行${checkName}测试：$($testGroup.Name)"
        $targets = [string[]]$testGroup.Targets
        & $pythonPath -X utf8 -B -u 'run_test.py' @targets
        if ($LASTEXITCODE -ne 0) { throw "${checkName}测试失败：$($testGroup.Name)，退出码 $LASTEXITCODE。" }
    }
} finally {
    Pop-Location
}
if ($Quick) {
    Write-Host '快速检查通过。'
} else {
    Write-Host '输入与任务核心检查通过。'
}
