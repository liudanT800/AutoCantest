@echo off
echo ================================
echo   停用自动回复规则
echo ================================
cd /d "%~dp0"

:: 停用后清空所有规则 & 待发送队列, 无需重启服务

..\.venv\Scripts\python.exe send_auto_reply.py --stop
pause
