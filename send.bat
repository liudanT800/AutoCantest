@echo off
cd /d "%~dp0"

:: 实际发送脚本: payload\send_payload.py
:: 报文配置修改: payload\payload.py
:: 双击 payload\send.bat 可直接查看参数说明

python D:\Code\TestHelper\AutoCantest\payload\send_payload.py
pause