# 在 Windows 侧以独立进程启动 WSL 中的流水线后端，不随启动它的终端或会话退出而结束
# 用法：pwsh -File scripts\start-detached.ps1 [-Distro Ubuntu-24.04]
param(
    [string]$Distro = 'Ubuntu-24.04'
)
$ErrorActionPreference = 'Stop'

# 由脚本所在位置推出仓库里的 start.sh，再换成 WSL 路径，仓库放在哪个盘都可以
$startWin = Join-Path $PSScriptRoot 'start.sh'
$startSh = ((& wsl.exe -d $Distro -e wslpath -a ($startWin -replace '\\', '/')) -join '').Trim()
if (-not $startSh) {
    throw "无法把 $startWin 转换为 WSL 路径，请确认发行版 $Distro 存在"
}
$serverLog = '~/jianzhi/feedback-pipeline/data/logs/server.log'

# 已在运行则不重复启动
$running = (& wsl.exe -d $Distro -e bash -c 'pgrep -f "[b]in/uvicorn app.main:app" >/dev/null && echo yes || echo no') -join ''
if ($running -match 'yes') {
    Write-Output '后端已在运行'
    exit 0
}

# 通过 WMI 创建进程：新进程由系统服务派生，不属于当前终端的进程树与作业对象
$cmd = "wsl.exe -d $Distro -e bash -ic `"mkdir -p ~/jianzhi/feedback-pipeline/data/logs; bash '$startSh' >> $serverLog 2>&1`""
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
if ($r.ReturnValue -ne 0) {
    throw "启动失败，返回码 $($r.ReturnValue)"
}
Write-Output "后端已在后台启动，进程号 $($r.ProcessId)，日志 $serverLog"
