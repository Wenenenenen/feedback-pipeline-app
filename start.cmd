@echo off
if not "%FEEDBACK_UTF8%"=="1" (chcp 65001 >nul & set "FEEDBACK_UTF8=1" & cmd /c ""%~f0" %*" & exit /b)
rem 上面一行只能用英文：先切到 UTF-8 代码页，再用新的 cmd 重新执行本脚本，整个文件才会按 UTF-8 解析
rem 双击在后台启动流水线后端：本窗口显示启动日志与启动成功标识，按任意键关闭，关闭后后端继续运行
rem 停止后端双击 stop.cmd；发行版不是 Ubuntu-24.04 时，在命令行执行 start.cmd 发行版名
setlocal
set "DISTRO=%~1"
if "%DISTRO%"=="" set "DISTRO=Ubuntu-24.04"

where pwsh >nul 2>nul
if errorlevel 1 (
  echo 未找到 pwsh，请先安装 PowerShell 7（winget install Microsoft.PowerShell）
  goto end
)
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-detached.ps1" -Distro "%DISTRO%"

:end
echo.
echo 按任意键关闭本窗口（后端在后台继续运行，停止请双击 stop.cmd）
pause >nul
