$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$projectPath = Split-Path -Parent $PSScriptRoot
$buildPath = Join-Path $projectPath 'build\native-runtime'
$sourcePath = Join-Path $projectPath 'selfhost\runtime'
$executablePath = Join-Path $projectPath 'selfhost\NoNo.exe'
$pythonPath = if (Test-Path -LiteralPath (Join-Path $projectPath '.venv\Scripts\python.exe')) {
    Join-Path $projectPath '.venv\Scripts\python.exe'
} else {
    (Get-Command python -CommandType Application | Select-Object -First 1).Source
}

# 直接定位编译器和 SDK，不要求用户预先打开 Visual Studio 开发者终端。
$vswherePath = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vswherePath)) {
    throw '需要安装 Visual Studio C++ 生成工具和 Windows SDK。'
}
$visualStudioPath = & $vswherePath -latest -products '*' `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $visualStudioPath) { throw '未找到 Visual Studio C++ 生成工具。' }
$toolsPath = (Get-ChildItem -LiteralPath (Join-Path $visualStudioPath 'VC\Tools\MSVC') -Directory |
    Sort-Object Name -Descending | Select-Object -First 1).FullName
$sdkPath = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots').KitsRoot10
$sdkVersion = (Get-ChildItem -LiteralPath (Join-Path $sdkPath 'Include') -Directory |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'um\Windows.h') } |
    Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1).Name
if (-not $sdkVersion) { throw '未找到 Windows SDK。' }

$compilerPath = Join-Path $toolsPath 'bin\Hostx64\x64\cl.exe'
$resourceCompilerPath = Join-Path $sdkPath "bin\$sdkVersion\x64\rc.exe"
$includes = @(
    (Join-Path $toolsPath 'include'),
    (Join-Path $sdkPath "Include\$sdkVersion\ucrt"),
    (Join-Path $sdkPath "Include\$sdkVersion\shared"),
    (Join-Path $sdkPath "Include\$sdkVersion\um"),
    $buildPath
)
$libraries = @(
    (Join-Path $toolsPath 'lib\x64'),
    (Join-Path $sdkPath "Lib\$sdkVersion\ucrt\x64"),
    (Join-Path $sdkPath "Lib\$sdkVersion\um\x64")
)
New-Item -ItemType Directory -Path $buildPath -Force | Out-Null
$iconPath = (Join-Path $projectPath 'icons\icon.ico').Replace('\', '/')
[IO.File]::WriteAllText((Join-Path $buildPath 'runtime_resources.h'),
    "#define APP_ICON `"$iconPath`"`n", [Text.UTF8Encoding]::new($false))
$resourcePath = Join-Path $buildPath 'host.res'
$resourceArguments = @('/nologo', '/c65001', '/fo', $resourcePath)
foreach ($include in $includes) { $resourceArguments += @('/I', $include) }
$resourceArguments += Join-Path $sourcePath 'host.rc'
& $resourceCompilerPath @resourceArguments
if ($LASTEXITCODE -ne 0) { throw "资源编译失败，退出码 $LASTEXITCODE。" }

# 静态链接宿主自身的 C 运行库，新增 EXE 不额外依赖机器上的 VC 运行库安装。
$compilerArguments = @('/nologo', '/utf-8', '/W4', '/WX', '/analyze', '/O2', '/MT', '/DUNICODE', '/D_UNICODE',
    "/Fe$executablePath", "/Fo$(Join-Path $buildPath 'host.obj')")
foreach ($include in $includes) { $compilerArguments += "/I$include" }
$compilerArguments += @((Join-Path $sourcePath 'host.c'), $resourcePath, '/link',
    '/SUBSYSTEM:WINDOWS', 'shell32.lib', 'user32.lib')
foreach ($library in $libraries) { $compilerArguments += "/LIBPATH:$library" }
& $compilerPath @compilerArguments
if ($LASTEXITCODE -ne 0) { throw "宿主 EXE 编译失败，退出码 $LASTEXITCODE。" }
$metadata = (Get-Item -LiteralPath $executablePath).VersionInfo
if ($metadata.ProductName -ne 'NoNo' -or $metadata.FileDescription -ne 'NoNo') {
    throw '宿主 EXE 的产品名或文件描述不符合 NoNo 品牌配置。'
}

Push-Location -LiteralPath $projectPath
try {
    & $pythonPath -B run_test.py tests.TestSelfhostSmoke.TestNativeRuntime
    if ($LASTEXITCODE -ne 0) { throw "宿主运行验证失败，退出码 $LASTEXITCODE。" }
} finally {
    Pop-Location
}
Write-Host "已生成并验证业务宿主：$executablePath"
