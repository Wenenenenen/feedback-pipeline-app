# 在 Windows 侧以独立进程启动 WSL 中的流水线后端，不随启动它的终端或会话退出而结束
# 用法：pwsh -File scripts/start-detached.ps1
$ErrorActionPreference = 'Stop'

$distro = 'Ubuntu-24.04'
$startSh = '/mnt/e/WorkSpace/jianzhi/feedback-pipeline-app/scripts/start.sh'
$serverLog = '~/jianzhi/feedback-pipeline/data/logs/server.log'

# 已在运行则不重复启动
$running = (& wsl.exe -d $distro -e bash -c 'pgrep -f "[b]in/uvicorn app.main:app" >/dev/null && echo yes || echo no') -join ''
if ($running -match 'yes') {
    Write-Output '后端已在运行'
    exit 0
}

# 通过 WMI 创建进程：新进程由系统服务派生，不属于当前终端的进程树与作业对象
$cmd = "wsl.exe -d $distro -e bash -ic `"mkdir -p ~/jianzhi/feedback-pipeline/data/logs; bash $startSh >> $serverLog 2>&1`""
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
if ($r.ReturnValue -ne 0) {
    throw "启动失败，返回码 $($r.ReturnValue)"
}
Write-Output "后端已在后台启动，进程号 $($r.ProcessId)，日志 $serverLog"
