"""
CAN 报文发送客户端
"""

from multiprocessing.connection import Client
import sys
from datetime import datetime
import re
import os

PIPE_ADDRESS = r'\\.\pipe\cantest_pipe'
AUTH_KEY = b'cantest'

# 动态确保脚本所在目录在 sys.path 中，以正确导入 payload.py
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# 确保 payload.py 存在，若不存在，自动从默认配置创建
payload_path = os.path.join(script_dir, "payload.py")
if not os.path.exists(payload_path):
    default_payload = """
        # CAN 报文发送配置 (已加入 .gitignore，本地修改不会被 Git 追踪)
        SEND_MODE = 1 # 1 or 2
        REPEAT_COUNT = 1000000
        SEND_DURATION_S = None  # 发送时间(秒)，与 REPEAT_COUNT 二选一，若设置了该值，则自动通过间隔和时长反推发送次数并覆盖 REPEAT_COUNT
        REPEAT_GROUP_INTERVAL_MS = 100
        REPEAT_FRAME_INTERVAL_MS = 50

        raw_frames_tmp = \"\"\"
            012345678  00 00 00 00 00 00 00 00
        \"\"\"

        raw_frames = raw_frames_tmp

        single_frame_duration_s = 5
        frame_interval_ms = 200
"""
    with open(payload_path, "w", encoding="utf-8") as f:
        f.write(default_payload)

try:
    from payload import (
        SEND_MODE,
        REPEAT_COUNT,
        SEND_DURATION_S,
        REPEAT_GROUP_INTERVAL_MS,
        REPEAT_FRAME_INTERVAL_MS,
        raw_frames,
        single_frame_duration_s,
        frame_interval_ms,
    )
except ImportError as e:
    print(f"错误: 导入 payload 失败: {e}")
    sys.exit(1)

# ============================================================================
# 报文解析清洗函数
# ============================================================================
def parse_raw_frame_line(line):
    # 1. 移除可能包含的括号及之后的注释内容
    line_clean = re.split(r'[(（]', line)[0].strip()
    
    # 2. 按空白字符分割
    parts = line_clean.split()
    if not parts:
        return None
        
    frame_id = parts[0]
    if not frame_id.startswith("0x"):
        frame_id = "0x" + frame_id
        
    # 3. 过滤并提取后续的有效十六进制字节 (最多8个字节)
    data_bytes = []
    for p in parts[1:]:
        if len(data_bytes) >= 8:
            break
        # 检查是否为合法的1或2位十六进制数
        if len(p) in (1, 2) and all(c in '0123456789abcdefABCDEF' for c in p):
            data_bytes.append(p)
        else:
            # 遇到非十六进制字符，直接截断
            break
            
    if not data_bytes:
        return None
        
    return frame_id, " ".join(data_bytes)

frames = []
for line in raw_frames.strip().split('\n'):
    parsed = parse_raw_frame_line(line)
    if parsed:
        frame_id, data = parsed
        
        def send_mode_2(): #发送一帧，持续 single_frame_duration_s (以 frame_interval_ms 为间隔周期发送)
            repeat_each = int(single_frame_duration_s * 1000 / frame_interval_ms)
            for _ in range(repeat_each):
                frames.append({"id": frame_id, "data": data})

        def send_mode_1(): #轮流发送所有帧,每帧间隔 single_frame_duration_s，每轮间隔 frame_interval_ms
            frames.append({"id": frame_id, "data": data})

        if SEND_MODE == 1:
            send_mode_1()
        elif SEND_MODE == 2:
            send_mode_2()

if SEND_DURATION_S is not None:
    if len(frames) > 0:
        one_group_duration_ms = (len(frames) - 1) * REPEAT_FRAME_INTERVAL_MS + REPEAT_GROUP_INTERVAL_MS
        if one_group_duration_ms > 0:
            target_time_ms = SEND_DURATION_S * 1000
            calculated_repeat_count = max(1, int(round((target_time_ms + REPEAT_GROUP_INTERVAL_MS) / one_group_duration_ms)))
            print(f"已设置发送时间 {SEND_DURATION_S}s，自动反推并覆盖组循环次数: {REPEAT_COUNT} -> {calculated_repeat_count}")
            REPEAT_COUNT = calculated_repeat_count
        else:
            REPEAT_COUNT = 1
    else:
        REPEAT_COUNT = 1

payload = {
    "action": "run",
    "repeat_count": REPEAT_COUNT,
    "group_interval_ms": REPEAT_GROUP_INTERVAL_MS,
    "frame_interval_ms": REPEAT_FRAME_INTERVAL_MS,
    "frames": frames,
}

# 打印发送模式与报文列表信息
print("=" * 60)
if SEND_MODE == 1:
    print(f"发送模式: [模式 1] 轮流发送所有帧")
    print(f"参数配置: 组循环次数={REPEAT_COUNT}, 组内帧间延迟={REPEAT_FRAME_INTERVAL_MS}ms, 组间延迟={REPEAT_GROUP_INTERVAL_MS}ms")
elif SEND_MODE == 2:
    print(f"发送模式: [模式 2] 逐帧重发持续发送")
    print(f"参数配置: 单帧持续={single_frame_duration_s}s, 周期发送间隔={frame_interval_ms}ms")
    print(f"          组循环次数={REPEAT_COUNT}, 组内帧间延迟={REPEAT_FRAME_INTERVAL_MS}ms, 组间延迟={REPEAT_GROUP_INTERVAL_MS}ms")
else:
    print(f"发送模式: 未知模式 ({SEND_MODE})")

unique_input_frames = []
for line in raw_frames.strip().split('\n'):
    parsed = parse_raw_frame_line(line)
    if parsed:
        fid, fdata = parsed
        unique_input_frames.append((fid, fdata))

print(f"待发送的报文列表 (共 {len(unique_input_frames)} 种报文):")
for idx, (fid, fdata) in enumerate(unique_input_frames, 1):
    print(f"  [{idx}]\t ID: {fid} | Data: {fdata}")
print(f"生成待发送队列总帧数: {len(frames)} 帧")
print("=" * 60)

conn = None
try:
    print(f"正在连接管道: {PIPE_ADDRESS} ...")
    conn = Client(PIPE_ADDRESS, authkey=AUTH_KEY)
    print("已连接，正在发送任务...")
    print(f"开始发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
    conn.send(payload)
    print("任务已下发，等待执行完毕...\n")
    result = conn.recv()
    print("服务返回结果:")
    for k, v in result.items():
        print(f"  {k}: {v}")
except KeyboardInterrupt:
    print("\n用户中止，正在中止操作...")
except EOFError:
    print("\n管道连接已断开")
except Exception as e:
    print(f"\n发生错误: {e}")
finally:
    if conn:
        try:
            conn.close()
        except Exception:
            pass