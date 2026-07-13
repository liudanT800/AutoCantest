@echo off
echo ================================
echo   启用自动回复规则
echo ================================
cd /d "%~dp0"

:: ========== 常用参数 (在 auto_reply_rules.py 中修改) ==========
:: 规则格式:
::   {"match_id": "0x5A5",         ← 匹配的 CAN ID
::    "match_pattern": "40 05 ...",← 数据模式, ** 为通配符, null=不校验
::    "reply_frames": [            ← 回复帧列表 (支持多帧 + 帧间延迟)
::       {"id": "0x5A6", "data": "4B 05 20 01 ...", "delay_ms": 0}
::    ]}
:: =============================================================

..\.venv\Scripts\python.exe send_auto_reply.py
pause
