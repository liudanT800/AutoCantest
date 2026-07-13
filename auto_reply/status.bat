@echo off
echo ================================
echo   自动回复状态
echo ================================
cd /d "%~dp0"

:: 返回: 是否启用 / 规则数 / 待发送队列长度

..\.venv\Scripts\python.exe send_auto_reply.py --status
pause
