@echo off
if not "%FEEDBACK_UTF8%"=="1" (chcp 65001 >nul & set "FEEDBACK_UTF8=1" & cmd /c ""%~f0" %*" & exit /b)
rem 上面一行只能用英文：先切到 UTF-8 代码页，再用新的 cmd 重新执行本脚本，整个文件才会按 UTF-8 解析
rem 双击在前台运行流水线后端：日志直接显示在本窗口，关闭窗口或按 Ctrl+C 即停止
rem 发行版不是 Ubuntu-24.04 时，在命令行执行 start.cmd 发行版名
setlocal
set "DISTRO=%~1"
if "%DISTRO%"=="" set "DISTRO=Ubuntu-24.04"
rem 仓库根目录即本脚本所在目录（去掉末尾的反斜杠）
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

rem 后端已在运行（例如由 scripts\start-detached.ps1 在后台启动）时不重复启动，避免端口冲突
wsl.exe -d %DISTRO% -e bash -c "pgrep -f '[b]in/uvicorn app.main:app' >/dev/null"
if not errorlevel 1 (
  echo 后端已在运行，浏览器打开 http://localhost:8765 即可
  pause
  exit /b 0
)

echo 正在前台启动后端，浏览器打开 http://localhost:8765 ，关闭本窗口或按 Ctrl+C 停止
wsl.exe -d %DISTRO% --cd "%ROOT%" -e bash -ic "bash scripts/start.sh"
echo.
echo 后端已退出
pause
