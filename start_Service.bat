@echo off
cd /d "%~dp0"

:: ========== 命令行参数 ==========
:: --dev-type  设备类型 (默认 4=USBCAN2)
:: --baud      波特率 kbps (默认 250, 常用 125/250/500)
:: --can-idx   通道索引 (0=CAN1, 1=CAN2)
:: --log-file  日志路径 (默认 logs/can_bus_<日期>.log)
:: ================================

.venv\Scripts\python.exe can_service.py --baud 125
pause