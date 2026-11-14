# 在 Windows 侧以独立进程启动 WSL 中的流水线后端，不随启动它的终端或会话退出而结束
# 启动后实时显示本次启动的日志，接口可访问时输出启动成功标识；失败或超时返回非 0
# 用法：pwsh -File scripts\start-detached.ps1 [-Distro Ubuntu-24.04] [-TimeoutSec 180]
param(
    [string]$Distro = 'Ubuntu-24.04',
    [int]$TimeoutSec = 180
)
$ErrorActionPreference = 'Stop'
# WSL 输出的是 UTF-8，按 UTF-8 解码才能正常显示中文日志
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$url = 'http://localhost:8765'

function Show-Banner([string]$Title) {
    Write-Host ''
    Write-Host '========================================' -ForegroundColor Green
    Write-Host "  $Title" -ForegroundColor Green
    Write-Host "  浏览器打开 $url" -ForegroundColor Green
    Write-Host '========================================' -ForegroundColor Green
}

# 由脚本所在位置推出仓库里的脚本，再换成 WSL 路径，仓库放在哪个盘都可以
function ConvertTo-WslPath([string]$WinPath) {
    $p = ((& wsl.exe -d $Distro -e wslpath -a ($WinPath -replace '\\', '/')) -join '').Trim()
    if (-not $p) {
        throw "无法把 $WinPath 转换为 WSL 路径，请确认发行版 $Distro 存在"
    }
    return $p
}
$startSh = ConvertTo-WslPath (Join-Path $PSScriptRoot 'start.sh')
$waitSh = ConvertTo-WslPath (Join-Path $PSScriptRoot 'wait-ready.sh')
$serverLog = '~/jianzhi/feedback-pipeline/data/logs/server.log'

# 已在运行则不重复启动
$running = (& wsl.exe -d $Distro -e bash -c 'pgrep -f "[b]in/uvicorn app.main:app" >/dev/null && echo yes || echo no') -join ''
if ($running -match 'yes') {
    Show-Banner '后端已在运行，无需重复启动'
    exit 0
}

# 记下启动前的日志行数，之后只显示本次启动新增的日志
$before = ((& wsl.exe -d $Distro -e bash -c "mkdir -p ~/jianzhi/feedback-pipeline/data/logs; touch $serverLog; wc -l < $serverLog") -join '').Trim()
if ($before -notmatch '^\d+$') { $before = '0' }

# 通过 WMI 创建进程：新进程由系统服务派生，不属于当前终端的进程树与作业对象
$cmd = "wsl.exe -d $Distro -e bash -ic `"bash '$startSh' >> $serverLog 2>&1`""
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
if ($r.ReturnValue -ne 0) {
    throw "启动失败，返回码 $($r.ReturnValue)"
}
Write-Host "后端已在后台启动，进程号 $($r.ProcessId)，日志 $serverLog"
Write-Host '启动日志：' -ForegroundColor DarkGray

& wsl.exe -d $Distro -e bash $waitSh $before $TimeoutSec
switch ($LASTEXITCODE) {
    0 { Show-Banner '后端启动成功'; exit 0 }
    1 { Write-Host ''; Write-Host "[失败] 后端进程已退出，请查看上面的日志或 $serverLog" -ForegroundColor Red; exit 1 }
    default { Write-Host ''; Write-Host "[超时] $TimeoutSec 秒内接口仍不可访问，请查看 $serverLog" -ForegroundColor Yellow; exit 2 }
}
