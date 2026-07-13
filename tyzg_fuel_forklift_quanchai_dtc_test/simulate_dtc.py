"""
太原重工 燃油叉车全柴协议故障码测试 - UDS DTC 诊断仿真器 (ECU 模拟端)

监听物理 CAN 总线，等待 T-BOX 触发 UDS 服务 19 诊断请求 (ID: 0x18DA00F1)，
并根据选择的场景自动进行应答交互：
- 场景 1：验证【无故障码】（单帧交互）
- 场景 2：验证【单故障码】（处理流控，首帧 -> 流控 -> 连续帧）
- 场景 3：验证【多故障码】（处理流控，首帧 -> 流控 -> 连续帧1 -> 延时10ms -> 连续帧2）
"""

import os
import sys
import time
import ctypes
import argparse
import signal
import threading
from datetime import datetime

# ============================================================================
# DLL 加载
# ============================================================================
def load_bridge_dll():
    """加载 my_can_bridge.dll 并声明所有 C 接口签名"""
    # 动态定位 bin 目录 (由于在子文件夹内，需要向上寻址一级)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dll_dir = os.path.join(os.path.dirname(script_dir), "bin")

    # 注册 DLL 搜索路径
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(dll_dir)
        except Exception:
            pass

    # 预加载官方依赖 ControlCAN.dll
    dep_path = os.path.join(dll_dir, "ControlCAN.dll")
    if os.path.exists(dep_path):
        try:
            ctypes.WinDLL(dep_path)
        except Exception:
            pass

    dll_path = os.path.join(dll_dir, "my_can_bridge.dll")
    if not os.path.exists(dll_path):
        raise FileNotFoundError(f"找不到包装动态链接库: {dll_path}，请先执行编译。")

    dll = ctypes.WinDLL(dll_path)

    # 声明接口签名
    dll.InitCanBridge.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    dll.InitCanBridge.restype = ctypes.c_int

    dll.SendCanHex.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_char_p, ctypes.c_int]
    dll.SendCanHex.restype = ctypes.c_int

    dll.FetchReceivedMessage.argtypes = [ctypes.c_int]
    dll.FetchReceivedMessage.restype = ctypes.c_char_p

    dll.SetChannelFilter.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.c_int]
    dll.SetChannelFilter.restype = None

    dll.CloseCanBridge.argtypes = [ctypes.c_int, ctypes.c_int]
    dll.CloseCanBridge.restype = ctypes.c_int

    return dll

# ============================================================================
# 报文解析与应答处理
# ============================================================================
def parse_rx_msg(msg_str):
    """
    解析接收到的报文文本
    格式: CAN0|ID:0x18DA00F1|Data:05 19 42 33 04 1E FF FF
    """
    try:
        parts = msg_str.strip().split('|')
        can_ch = parts[0]
        msg_id_str = parts[1].split(':')[1]  # "0x18DA00F1"
        msg_id = int(msg_id_str, 16)
        data_str = parts[2].split(':')[1]    # "05 19 42 33 04 1E FF FF"
        data_bytes = bytes.fromhex(data_str.replace(' ', ''))
        return msg_id, data_bytes, data_str
    except Exception:
        return None, None, None

def handle_request(dll, dev_type, can_idx, scenario):
    """
    诊断请求应答处理状态机
    """
    tx_id = 0x18DAF100

    if scenario == 1:
        # 场景 1：验证【无故障码】（单帧交互）
        # 立即发送：18DAF100  06 59 42 33 04 1E 00 00
        tx_data = b"06 59 42 33 04 1E 00 00"
        ret = dll.SendCanHex(dev_type, tx_id, tx_data, can_idx)
        ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        if ret == 1:
            print(f"[{ts}] [TX] 立即发送单帧响应: ID=0x{tx_id:08X} Data={tx_data.decode('utf-8')}")
            print("===> [SUCCESS] 场景 1 (无故障码) 交互完成！")
        else:
            print(f"[{ts}] [ERROR] 发送单帧响应失败！")

    elif scenario == 2:
        # 场景 2：验证【单故障码】（流控处理）
        # 1. 立即发送响应首帧：18DAF100  10 0B 59 42 33 04 1E 00
        ff_data = b"10 0B 59 42 33 04 1E 00"
        ret = dll.SendCanHex(dev_type, tx_id, ff_data, can_idx)
        ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        if ret != 1:
            print(f"[{ts}] [ERROR] 发送响应首帧失败！")
            return
        print(f"[{ts}] [TX] 立即发送响应首帧: ID=0x{tx_id:08X} Data={ff_data.decode('utf-8')}")

        # 2. 停止发送，等待总线上出现 T-BOX 回复的流控帧：18DA00F1  30 00 0A FF FF FF FF FF
        print("Waiting... 等待 T-BOX 回复流控帧 (18DA00F1，且数据以 30 开头)...")
        fc_received = False
        timeout_time = time.time() + 2.0  # 2秒超时限制
        while time.time() < timeout_time:
            msg_ptr = dll.FetchReceivedMessage(can_idx)
            if msg_ptr:
                msg_str = msg_ptr.decode('utf-8', errors='ignore')
                rx_id, rx_bytes, rx_str = parse_rx_msg(msg_str)
                if rx_id == 0x18DA00F1:
                    # 判断是否为流控帧 (PCI 字节高 4 位为 3)
                    if len(rx_bytes) > 0 and (rx_bytes[0] & 0xF0) == 0x30:
                        ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                        print(f"[{ts}] [RX] 监测到流控帧: ID=0x{rx_id:08X} Data={rx_str}")
                        fc_received = True
                        break
            time.sleep(0.001)

        # 3. 监测到上述流控帧后，立即发送响应连续帧：18DAF100  21 01 12 34 56 AA FF FF
        if fc_received:
            cf_data = b"21 01 12 34 56 AA FF FF"
            ret = dll.SendCanHex(dev_type, tx_id, cf_data, can_idx)
            ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            if ret == 1:
                print(f"[{ts}] [TX] 立即发送响应连续帧: ID=0x{tx_id:08X} Data={cf_data.decode('utf-8')}")
                print("===> [SUCCESS] 场景 2 (单故障码) 交互完成！")
            else:
                print(f"[{ts}] [ERROR] 发送连续帧失败！")
        else:
            print("===> [FAIL] 等待 T-BOX 流控帧超时，交互未完成。")

    elif scenario == 3:
        # 场景 3：验证【多故障码】（流控处理，双连续帧）
        # 1. 立即发送响应首帧：18DAF100  10 10 59 42 33 04 1E 00
        ff_data = b"10 10 59 42 33 04 1E 00"
        ret = dll.SendCanHex(dev_type, tx_id, ff_data, can_idx)
        ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        if ret != 1:
            print(f"[{ts}] [ERROR] 发送响应首帧失败！")
            return
        print(f"[{ts}] [TX] 立即发送响应首帧: ID=0x{tx_id:08X} Data={ff_data.decode('utf-8')}")

        # 2. 等待总线上出现 T-BOX 回复的流控帧：18DA00F1  30 00 0A FF FF FF FF FF
        print("Waiting... 等待 T-BOX 回复流控帧 (18DA00F1，且数据以 30 开头)...")
        fc_received = False
        timeout_time = time.time() + 2.0  # 2秒超时限制
        while time.time() < timeout_time:
            msg_ptr = dll.FetchReceivedMessage(can_idx)
            if msg_ptr:
                msg_str = msg_ptr.decode('utf-8', errors='ignore')
                rx_id, rx_bytes, rx_str = parse_rx_msg(msg_str)
                if rx_id == 0x18DA00F1:
                    if len(rx_bytes) > 0 and (rx_bytes[0] & 0xF0) == 0x30:
                        ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                        print(f"[{ts}] [RX] 监测到流控帧: ID=0x{rx_id:08X} Data={rx_str}")
                        fc_received = True
                        break
            time.sleep(0.001)

        if fc_received:
            # 3. 监测到流控帧后，连续发送两条帧
            # 先发连续帧 1：18DAF100  21 01 12 34 56 AA 02 AB
            cf1_data = b"21 01 12 34 56 AA 02 AB"
            ret1 = dll.SendCanHex(dev_type, tx_id, cf1_data, can_idx)
            ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            if ret1 == 1:
                print(f"[{ts}] [TX] 立即发送连续帧1: ID=0x{tx_id:08X} Data={cf1_data.decode('utf-8')}")
            else:
                print(f"[{ts}] [ERROR] 发送连续帧1失败！")
                return

            # 延时 10ms
            time.sleep(0.01)

            # 再发连续帧 2：18DAF100  22 CD EF 55 FF FF FF FF
            cf2_data = b"22 CD EF 55 FF FF FF FF"
            ret2 = dll.SendCanHex(dev_type, tx_id, cf2_data, can_idx)
            ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            if ret2 == 1:
                print(f"[{ts}] [TX] 延时 10ms 发送连续帧2: ID=0x{tx_id:08X} Data={cf2_data.decode('utf-8')}")
                print("===> [SUCCESS] 场景 3 (多故障码) 交互完成！")
            else:
                print(f"[{ts}] [ERROR] 发送连续帧2失败！")
        else:
            print("===> [FAIL] 等待 T-BOX 流控帧超时，交互未完成。")

# ============================================================================
# 后台转速报文发送线程
# ============================================================================
def bg_sender_thread(dll, dev_type, can_idx, stop_event):
    """
    后台持续发送发动机转速报文
    报文 ID: 0x0CF00400 (EEC1)
    报文 Data: FF FF 7E 10 27 FF FF FF (表示 1250 rpm)
    发送周期: 20ms
    """
    bg_id = 0x0CF00400
    bg_data = b"FF FF 7E 10 27 FF FF FF"
    bg_interval = 0.02  # 20ms
    while not stop_event.is_set():
        try:
            dll.SendCanHex(dev_type, bg_id, bg_data, can_idx)
        except Exception:
            pass
        time.sleep(bg_interval)

# ============================================================================
# 主入口
# ============================================================================
def main():
    parser = argparse.ArgumentParser(description="太原重工 燃油叉车全柴协议故障码测试工具")
    parser.add_argument("--dev-type", type=int, default=4, help="设备类型 (默认 4 = USBCAN2)")
    parser.add_argument("--baud", type=int, default=250, help="波特率 kbps (默认 250)")
    parser.add_argument("--can-idx", type=int, default=0, help="通道索引 (默认 0 = CAN1)")
    parser.add_argument("--scenario", type=int, choices=[1, 2, 3], help="验证场景：1 (无故障码), 2 (单故障码), 3 (多故障码)")
    args = parser.parse_args()

    # 1. 引导用户选择场景 (若命令行未指定)
    scenario = args.scenario
    if scenario is None:
        print("=" * 60)
        print("  太原重工 燃油叉车全柴协议故障码测试")
        print("=" * 60)
        print(" [1] 验证【无故障码】 (单帧交互)")
        print(" [2] 验证【单故障码】 (处理一次流控)")
        print(" [3] 验证【多故障码】 (处理流控 + 两条连续帧)")
        print("-" * 60)
        try:
            val = input("请选择验证场景 (输入序号 1/2/3): ").strip()
            if val not in ('1', '2', '3'):
                print("输入有误，默认选择 1")
                scenario = 1
            else:
                scenario = int(val)
        except (KeyboardInterrupt, SystemExit):
            print("\n已退出。")
            sys.exit(0)

    # 2. 加载 DLL 接口
    try:
        dll = load_bridge_dll()
    except Exception as e:
        print(f"[FATAL] 加载桥接 DLL 失败: {e}")
        sys.exit(1)

    # 3. 初始化 CAN 通道
    print(f"\n正在初始化 CAN 设备 (DevType={args.dev_type}, Baud={args.baud}k, Channel=CAN{args.can_idx + 1})...")
    init_ret = dll.InitCanBridge(args.dev_type, args.baud, args.can_idx)
    if init_ret != 1:
        print("[FATAL] InitCanBridge 失败，请检查硬件物理连接，或确认后台 can_service.py 服务已关闭。")
        sys.exit(1)
    print("[SUCCESS] CAN 设备初始化成功。")

    # 启动后台发送转速报文的线程
    bg_stop_event = threading.Event()
    bg_thread = threading.Thread(
        target=bg_sender_thread,
        args=(dll, args.dev_type, args.can_idx, bg_stop_event),
        name="BGSpeedSender",
        daemon=True
    )
    bg_thread.start()
    print("[SPEED] 已启动后台转速报文持续发送线程 (ID: 0x0CF00400, 间隔: 20ms)。")

    # 4. 配置软件过滤以保证接收效率 (仅保留 ID 0x18DA00F1)
    filter_ids = [0x18DA00F1]
    id_array = (ctypes.c_uint * len(filter_ids))(*filter_ids)
    dll.SetChannelFilter(args.can_idx, id_array, len(filter_ids))
    print(f"[FILTER] 已设置 ID 过滤器，只接收: {[hex(x) for x in filter_ids]}")

    # 注册 Ctrl+C 退出处理
    stop_flag = False
    def sigint_handler(signum, frame):
        nonlocal stop_flag
        stop_flag = True
        print("\n正在停止并关闭 CAN 通道...")
        bg_stop_event.set()

    signal.signal(signal.SIGINT, sigint_handler)

    # 5. 主循环监听总线请求
    print("\n" + "=" * 60)
    print(f" 正在后台监听总线，等待 T-BOX 发起诊断请求... (按 Ctrl+C 退出)")
    print(f" 当前所选验证场景: [场景 {scenario}]")
    print("=" * 60)

    try:
        while not stop_flag:
            msg_ptr = dll.FetchReceivedMessage(args.can_idx)
            if msg_ptr:
                msg_str = msg_ptr.decode('utf-8', errors='ignore')
                rx_id, rx_bytes, rx_str = parse_rx_msg(msg_str)
                if rx_id == 0x18DA00F1:
                    # 校验是否为 UDS 诊断请求，且包含 "19 42 33 04 1E"
                    # 在 UDS 帧中，数据第 1 字节通常是长度 PCI 或者是子服务号
                    # 报文示例: 18DA00F1  05 19 42 33 04 1E FF FF
                    if rx_bytes and len(rx_bytes) >= 6:
                        if rx_bytes[1:6] == b'\x19\x42\x33\x04\x1e' or b'\x19\x42\x33\x04\x1e' in rx_bytes:
                            print(f"\n[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] [RX] 收到匹配的 T-BOX 诊断请求: ID=0x{rx_id:08X} Data={rx_str}")
                            handle_request(dll, args.dev_type, args.can_idx, scenario)
                            time.sleep(0.5)
            else:
                time.sleep(0.005)
    except KeyboardInterrupt:
        pass
    finally:
        # 安全关闭
        bg_stop_event.set()
        bg_thread.join(timeout=1.0)
        dll.CloseCanBridge(args.dev_type, args.can_idx)
        print("[EXIT] CAN 通道已安全释放，仿真退出。")

if __name__ == "__main__":
    main()
