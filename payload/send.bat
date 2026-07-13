@echo off
cd /d "%~dp0"

:: ========== 常用参数 (在 payload.py 中修改) ==========
:: SEND_MODE           1=轮流发送  2=单帧重复
:: REPEAT_COUNT        组循环次数 (如 1000000)
:: SEND_DURATION_S     发送时长秒, 与 REPEAT_COUNT 二选一, 设置后自动反推次数
:: REPEAT_GROUP_INTERVAL_MS  组间延迟 ms
:: REPEAT_FRAME_INTERVAL_MS  帧间延迟 ms
:: SINGLE_FRAME_DURATION_S   模式2单帧持续秒
:: FRAME_INTERVAL_MS         模式2发送间隔 ms
:: raw_frames          选择发送的报文组, 如:
::     Frames.NobleLink.SDO_TRACTION_QUERY
::     Frames.NobleLink.SDO_ALL
::     Frames.NobleLink.STATUS
::     Frames.NobleLink.ALARM
::     Frames.TMP
:: =====================================================

..\.venv\Scripts\python.exe send_payload.py
pause
