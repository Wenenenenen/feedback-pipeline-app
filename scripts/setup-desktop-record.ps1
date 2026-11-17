# 安装本机桌面录屏需要的 Windows 侧依赖（在 Windows 中执行，不需要管理员权限）
#   playwright-core：执行器连接本机 Chrome 读取页面状态（装在 %LOCALAPPDATA%\feedback-record\node）
#   pyaudiowpatch：用 WASAPI 回环录下系统声音（ffmpeg 只能录麦克风）
# 另外需要：Google Chrome、Windows Terminal、ffmpeg（winget install Gyan.FFmpeg）、Python 3、node
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$root = Join-Path $env:LOCALAPPDATA 'feedback-record'
$nodeDir = Join-Path $root 'node'
New-Item -ItemType Directory -Force -Path $nodeDir | Out-Null
Push-Location $nodeDir
try {
    if (-not (Test-Path 'package.json')) { '{"name":"feedback-record","private":true}' | Set-Content -Encoding utf8 package.json }
    npm install --no-fund --no-audit playwright-core@1.65.1
    if ($LASTEXITCODE -ne 0) { throw 'playwright-core 安装失败' }
} finally {
    Pop-Location
}

python -m pip install --user --disable-pip-version-check pyaudiowpatch
if ($LASTEXITCODE -ne 0) { throw 'pyaudiowpatch 安装失败' }

Write-Output "已安装："
Write-Output ("playwright-core " + (Get-Content (Join-Path $nodeDir 'node_modules\playwright-core\package.json') | ConvertFrom-Json).version)
python -c "import pyaudiowpatch; print('pyaudiowpatch', pyaudiowpatch.__version__)"
