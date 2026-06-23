"""
CAN 报文发送客户端
"""

from multiprocessing.connection import Client
import sys

PIPE_ADDRESS = r'\\.\pipe\cantest_pipe'
AUTH_KEY = b'cantest'

SEND_MODE = 1 # 1 or 2
REPEAT_COUNT = 100000
REPEAT_GROUP_INTERVAL_MS = 100
REPEAT_FRAME_INTERVAL_MS = 10

raw_frames_tmp = """
18ecff00 20 0A 00 02 FF CA FE 00
18ebff00 01 40 FF CE 0C 00 04 55
18ebff00 02 F1 E0 05 FF FF FF FF
"""

raw_frames_status = """
    18FEF500 0A FF FF 10 27 FF FF FF
    0CF00400 FF FF 7E 50 01 FF FF FF
    18FEDF00 7E FF FF FF FF FF FF FF
    18FEF200 10 27 FF FF FF FF FF FF
    0CF00A00 FF FF 10 27 FF FF FF FF
    18FDB200 FF FF FF FF 0A 00 FF FF
    18FEEE00 2A FF FF FF FF FF FF FF
    0CFF0F00 FF 0A FF FF FF FF FF FF
    18FD9400 10 27 FF FF FF FF FF FF
    18FDD500 FF FF FF FF 10 27 FF FF
"""

raw_frames_alarm = """
    226 FF FF FF FF FF FF FF 59
    226 FF FF FF FF FF FF FF 60
    226 FF FF FF FF FF FF FF 61
    226 FF FF FF FF FF FF FF 62
    226 FF FF FF FF FF FF FF 63
    226 FF FF FF FF FF FF FF 64
    226 FF FF FF FF FF FF FF 65
    226 FF FF FF FF FF FF FF 66
    226 FF FF FF FF FF FF FF 67
    226 FF FF FF FF FF FF FF 68
    226 FF FF FF FF FF FF FF 69
    226 FF FF FF FF FF FF FF 6A
    226 FF FF FF FF FF FF FF 6B
    226 FF FF FF FF FF FF FF 6C
    226 FF FF FF FF FF FF FF 6D
    226 FF FF FF FF FF FF FF 6E
    226 FF FF FF FF FF FF FF 6F
    226 FF FF FF FF FF FF FF 70
    226 FF FF FF FF FF FF FF 71
    226 FF FF FF FF FF FF FF 72
    226 FF FF FF FF FF FF FF 73
"""

raw_frames = raw_frames_tmp

single_frame_duration_s = 5
frame_interval_ms = 200

frames = []
for line in raw_frames.strip().split('\n'):
    parts = line.split(maxsplit=1)
    if len(parts) == 2:
        frame_id = parts[0].strip()
        if frame_id[:2] != "0x":
            frame_id = "0x" + frame_id
        data = parts[1].strip()
        
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
    parts = line.split(maxsplit=1)
    if len(parts) == 2:
        fid = parts[0].strip()
        if not fid.startswith("0x"):
            fid = "0x" + fid
        fdata = parts[1].strip()
        unique_input_frames.append((fid, fdata))

print(f"待发送的报文列表 (共 {len(unique_input_frames)} 种报文):")
for idx, (fid, fdata) in enumerate(unique_input_frames, 1):
    print(f"  [{idx}] ID: {fid} | Data: {fdata}")
print(f"生成待发送队列总帧数: {len(frames)} 帧")
print("=" * 60)

conn = None
try:
    print(f"正在连接管道: {PIPE_ADDRESS} ...")
    conn = Client(PIPE_ADDRESS, authkey=AUTH_KEY)
    print("已连接，正在发送任务...")
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
