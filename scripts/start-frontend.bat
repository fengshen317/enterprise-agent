@echo off
chcp 65001 >nul
setlocal
rem 使用脚本位置定位前端，避免依赖启动目录。
pushd "%~dp0..\frontend" || goto :failed
where node >nul 2>&1 || goto :missing_node
where npm.cmd >nul 2>&1 || goto :missing_node
if not exist node_modules (
  call npm.cmd ci
  if errorlevel 1 goto :failed
)
echo 启动前端：http://127.0.0.1:8686（Ctrl+C 停止）
call npm.cmd run dev
if errorlevel 1 goto :failed
popd
exit /b 0

:missing_node
echo 请先安装 Node.js 22.12+ 和 npm，并加入 PATH。
goto :failed

:failed
echo 前端启动失败，请检查上方错误信息。
pause
exit /b 1
