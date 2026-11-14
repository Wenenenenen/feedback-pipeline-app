@echo off
if not "%FEEDBACK_UTF8%"=="1" (chcp 65001 >nul & set "FEEDBACK_UTF8=1" & cmd /c ""%~f0" %*" & exit /b)
rem 上面一行只能用英文：先切到 UTF-8 代码页，再用新的 cmd 重新执行本脚本，整个文件才会按 UTF-8 解析
rem 双击停止后台运行的流水线后端；有任务正在运行时先列出来，确认后才强制停止
rem 发行版不是 Ubuntu-24.04 时，在命令行执行 stop.cmd 发行版名
setlocal
set "DISTRO=%~1"
if "%DISTRO%"=="" set "DISTRO=Ubuntu-24.04"
rem 仓库根目录即本脚本所在目录（去掉末尾的反斜杠）
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

wsl.exe -d %DISTRO% --cd "%ROOT%" -e bash scripts/stop.sh
if not errorlevel 3 goto end

echo.
choice /c YN /n /m "仍要停止吗？按 Y 强制停止，按 N 取消："
if errorlevel 2 (
  echo 已取消，后端继续运行
  goto end
)
wsl.exe -d %DISTRO% --cd "%ROOT%" -e bash scripts/stop.sh --force

:end
echo.
echo 按任意键关闭本窗口
pause >nul
