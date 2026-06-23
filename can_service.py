"""
CAN 测试常驻服务 (命名管道版本)

通过 Windows 命名管道 (Named Pipe) 接收测试任务并驱动 CAN 硬件发送报文。
客户端断开连接时，当前发送任务会被自动中止 ("脱手即停")。

用法:
    python can/can_service.py
    python can/can_service.py --dev-type 4 --baud 250 --can-idx 0 --log-file can/can_bus.log
"""

import os
import sys
import ctypes
import time
import json
import argparse
import signal
import threading
import logging
from multiprocessing.connection import Listener
from datetime import datetime

# ============================================================================
# config
# ============================================================================

# -- 硬件 & 通道 --
DEFAULT_DEV_TYPE   = 4           # 设备类型: 4 = USBCAN2
DEFAULT_BAUD_RATE  = 250         # 波特率 (kbps)
DEFAULT_CAN_IDX    = 0           # CAN 通道索引: 0 = CAN1, 1 = CAN2

# -- 命名管道 --
DEFAULT_PIPE_ADDRESS = r'\\.\pipe\cantest_pipe'
PIPE_AUTH_KEY        = b'cantest'

# -- 日志 --
DEFAULT_LOG_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")  # 日志目录
DEFAULT_LOG_FILE   = None  # 运行时自动生成: logs/can_bus_YYYY-MM-DD.log
DEFAULT_LOG_LEVEL  = logging.DEBUG  # 日志级别 (DEBUG / INFO / WARNING)

# -- 运行时内部参数 --
RX_POLL_INTERVAL_S        = 0.005    # 接收线程空闲轮询间隔 (秒)
DEFAULT_TX_DATA           = "00 00 00 00 00 00 00 00"  # 帧数据缺省值 (8字节)
DEFAULT_EXPECT_TIMEOUT_MS = 2000   # expect 匹配默认超时 (毫秒)


# ============================================================================
# 日志配置
# ============================================================================
logger = logging.getLogger("CAN_Service")
logger.setLevel(DEFAULT_LOG_LEVEL)

# 自定义毫秒精度格式化器
class _MillisecondFormatter(logging.Formatter):
    """让 logging 的时间戳精确到毫秒 (如 14:10:40.123)"""
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created)
        if datefmt:
            s = ct.strftime(datefmt)
        else:
            s = ct.strftime("%H:%M:%S")
        return f"{s}.{int(record.msecs):03d}"

# 控制台输出
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.DEBUG)
_console_handler.setFormatter(_MillisecondFormatter(
    "[%(asctime)s] %(levelname)-7s %(message)s"
))
logger.addHandler(_console_handler)


# ============================================================================
# DLL 加载
# ============================================================================
def load_bridge_dll():
    """加载 my_can_bridge.dll 并声明所有 C 接口签名"""
    dll_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")

    # 注册 DLL 搜索路径 (Python 3.8+ 要求)
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

    # --- 接口签名声明 ---
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
# 全局共享状态 (跨线程)
# ============================================================================
class SharedState:
    """线程安全的共享状态容器，用于接收线程与任务处理线程之间通信"""

    def __init__(self):
        self.lock = threading.Lock()
        # --- expect 相关 ---
        self.expect_id: int | None = None
        self.expect_pattern: str | None = None
        self.expect_matched: bool = False
        self.expect_matched_msg: str = ""
        # --- 用于通知阻塞等待的处理线程 ---
        self.expect_event = threading.Event()
        # --- 用于中止当前的发送任务 ---
        self.abort_event = threading.Event()
        # --- 防止多个 run 任务并发执行 ---
        self.run_lock = threading.Lock()

    def set_expect(self, expect_id: int | None, pattern: str | None):
        """设置新一轮的期望匹配参数（由任务处理线程调用）"""
        with self.lock:
            self.expect_id = expect_id
            self.expect_pattern = pattern
            self.expect_matched = False
            self.expect_matched_msg = ""
            self.expect_event.clear()

    def clear_expect(self):
        """清除期望匹配状态"""
        with self.lock:
            self.expect_id = None
            self.expect_pattern = None
            self.expect_matched = False
            self.expect_matched_msg = ""
            self.expect_event.clear()

    def check_and_match(self, msg_str: str):
        """尝试将收到的报文与当前 expect 规则进行匹配（由接收线程调用）"""
        with self.lock:
            if self.expect_id is None or self.expect_matched:
                return  # 没有待匹配的规则，或已经匹配成功

            try:
                if "ID:" not in msg_str:
                    return
                id_part = msg_str.split("ID:")[1].split("|")[0]
                msg_id = int(id_part, 16) if id_part.lower().startswith("0x") else int(id_part)

                if msg_id != self.expect_id:
                    return

                if self.expect_pattern and not _match_data_pattern(msg_str, self.expect_pattern):
                    return

                # 匹配成功！
                self.expect_matched = True
                self.expect_matched_msg = msg_str
                self.expect_event.set()  # 唤醒阻塞等待的处理线程
            except Exception:
                pass


def _match_data_pattern(data_str: str, pattern: str) -> bool:
    """
    匹配报文数据是否符合特定模式。
    data_str: 例如 "CAN0|ID:0x18070190|Data:01 07 10 80 65 01 00 00"
    pattern:  例如 "01 07 10 80 65 01 ** **" (支持通配符 ** 或 *)
    """
    if not pattern:
        return True
    if "Data:" not in data_str:
        return False

    actual_data = data_str.split("Data:")[1].strip()
    actual_bytes = actual_data.split()
    pattern_bytes = pattern.split()

    if len(actual_bytes) < len(pattern_bytes):
        return False

    for a, p in zip(actual_bytes, pattern_bytes):
        if p in ("**", "*"):
            continue
        if a.lower() != p.lower():
            return False
    return True


def _parse_id(raw_id) -> int:
    """将十六进制或十进制的 ID 字符串/数字统一转为 int"""
    if isinstance(raw_id, str):
        return int(raw_id, 16) if raw_id.lower().startswith("0x") else int(raw_id)
    return int(raw_id)


# ============================================================================
# 线程 2：CAN 总线接收器 (后台守护线程)
# ============================================================================
def receiver_thread_func(dll, can_idx: int, state: SharedState, log_file: str, stop_event: threading.Event):
    """
    死循环调用 FetchReceivedMessage，实时打印 + 写日志 + 比对 expect。
    当 stop_event 被设置时，线程退出。
    """
    # 确保日志目录存在
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    logger.info(f"[接收线程] 总线接收器已启动，通道 CAN{can_idx + 1}，日志文件: {log_file}")

    # 打开日志文件 (追加写入)
    fh = open(log_file, "a", encoding="utf-8", buffering=1)  # line-buffered

    try:
        while not stop_event.is_set():
            msg_ptr = dll.FetchReceivedMessage(can_idx)
            if msg_ptr:
                try:
                    msg_str = msg_ptr.decode("utf-8")
                except UnicodeDecodeError:
                    msg_str = msg_ptr.decode("gbk", errors="replace")

                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                log_line = f"[{ts}] [RX] {msg_str}"
                logger.info(f"[RX] {msg_str}")
                fh.write(log_line + "\n")

                # 尝试与 expect 规则比对
                state.check_and_match(msg_str)
            else:
                # 队列为空，短暂休眠以避免占满 CPU
                time.sleep(RX_POLL_INTERVAL_S)
    finally:
        fh.close()
        logger.info("[接收线程] 总线接收器已停止。")


# ============================================================================
# 任务执行引擎 (从 HTTP 版本提取的核心逻辑)
# ============================================================================
def execute_run_task(dll, state: SharedState, cfg: dict, task: dict) -> dict:
    """
    执行一个发送任务，返回结果字典。
    此函数在任务处理线程中运行，会检查 state.abort_event 以支持中途中止。
    """
    can_idx = cfg["can_idx"]
    dev_type = cfg["dev_type"]

    # ---- 提取任务参数 ----
    repeat_count      = task.get("repeat_count", 1)
    group_interval_ms = task.get("group_interval_ms", 100)
    frame_interval_ms = task.get("frame_interval_ms", 20)
    frames            = task.get("frames", [])
    filter_ids_raw    = task.get("filter_ids", [])
    expect_config     = task.get("expect", None)

    if not frames:
        return {"status": "ERROR", "message": "任务中缺少 'frames' 字段或帧列表为空。"}

    task_start = time.time()
    state.abort_event.clear()

    # ---- 配置软件过滤器 ----
    if filter_ids_raw:
        id_ints = []
        for raw_id in filter_ids_raw:
            try:
                id_ints.append(_parse_id(raw_id))
            except Exception:
                logger.warning(f"[过滤器] ID 解析失败，跳过: {raw_id}")
        if id_ints:
            id_array = (ctypes.c_uint * len(id_ints))(*id_ints)
            dll.SetChannelFilter(can_idx, id_array, len(id_ints))
            logger.info(f"[过滤器] 已设置，仅接收 ID: {[hex(x) for x in id_ints]}")
    else:
        # 清除过滤器，接收全部报文
        dll.SetChannelFilter(can_idx, None, 0)

    # ---- 配置 expect 匹配规则 ----
    if expect_config:
        try:
            eid = _parse_id(expect_config.get("id"))
            epat = expect_config.get("data_pattern")
            state.set_expect(eid, epat)
            logger.info(f'[EXPECT] 设定比对目标: ID=0x{eid:X}, Pattern="{epat}"')
        except Exception as e:
            logger.error(f"[EXPECT] 期望报文配置解析失败: {e}")
            state.clear_expect()
    else:
        state.clear_expect()

    # ---- 执行发送循环 ----
    total_sent = 0
    for g_idx in range(repeat_count):
        if state.abort_event.is_set():
            logger.info("[TX] 发送任务收到中止指令，停止发送！")
            break

        logger.info(f"[TX] === 第 {g_idx + 1}/{repeat_count} 组报文 ===")

        for f_idx, frame in enumerate(frames):
            if state.abort_event.is_set():
                break

            try:
                id_val = _parse_id(frame.get("id"))
            except Exception:
                logger.error(f"[TX] 报文 ID 解析错误: {frame.get('id')}")
                continue

            data_str = frame.get("data", DEFAULT_TX_DATA)
            tx_data = data_str.encode("utf-8")

            logger.info(f'[TX] Frame {f_idx + 1}: ID=0x{id_val:X}, Data="{data_str}"')
            ret = dll.SendCanHex(dev_type, id_val, tx_data, can_idx)
            if ret == 1:
                total_sent += 1
            else:
                logger.warning(f"[TX] SendCanHex 返回失败 (ret={ret})")

            # 帧间延迟
            if frame_interval_ms > 0 and f_idx < len(frames) - 1:
                time.sleep(frame_interval_ms / 1000.0)

        # 组间延迟
        if group_interval_ms > 0 and g_idx < repeat_count - 1:
            time.sleep(group_interval_ms / 1000.0)

    # ---- 判定结果 ----
    result_status = "NO_EXPECTATION"
    result_message = f"发送完毕，共发送 {total_sent} 帧。"

    if state.abort_event.is_set():
        result_status = "ABORTED"
        result_message = f"任务被中止，共发送 {total_sent} 帧。"
    elif expect_config:
        timeout_ms = expect_config.get("timeout_ms", DEFAULT_EXPECT_TIMEOUT_MS)
        timeout_s = timeout_ms / 1000.0

        # 可能在发送循环期间已经匹配了
        with state.lock:
            already_matched = state.expect_matched

        if already_matched:
            with state.lock:
                result_status = "PASS"
                result_message = f"成功捕获到符合预期的目标报文: {state.expect_matched_msg}"
        else:
            logger.info(f"[EXPECT] 等待匹配，超时 {timeout_s}s ...")
            matched = state.expect_event.wait(timeout=timeout_s)

            with state.lock:
                if state.expect_matched:
                    result_status = "PASS"
                    result_message = f"成功捕获到符合预期的目标报文: {state.expect_matched_msg}"
                else:
                    result_status = "FAIL"
                    result_message = f"超时 ({timeout_ms}ms)，未捕获到预期报文。"

        state.clear_expect()

    duration_s = round(time.time() - task_start, 3)

    # 打印醒目的报告
    if result_status == "PASS":
        logger.info(f"[PASS] ======== {result_message} ========")
    elif result_status == "FAIL":
        logger.warning(f"[FAIL] ======== {result_message} ========")
    elif result_status == "ABORTED":
        logger.warning(f"[ABORTED] ======== {result_message} ========")
    else:
        logger.info(f"[DONE] {result_message}")

    return {
        "status": result_status,
        "message": result_message,
        "duration_s": duration_s,
        "frames_sent": total_sent,
    }


# ============================================================================
# 管道连接处理
# ============================================================================
def _watchdog_func(conn, state: SharedState):
    """
    看门狗线程：阻塞在 conn.recv() 上，
    - 如果收到 {"action": "abort"}，设置中止标志。
    - 如果客户端断开 (EOFError)，设置中止标志。
    这是 "脱手即停" 的核心保障。
    """
    try:
        while True:
            try:
                extra_msg = conn.recv()
                if isinstance(extra_msg, dict) and extra_msg.get("action") == "abort":
                    logger.info("[PIPE] 客户端通过管道发送了中止指令")
                    state.abort_event.set()
                    return
            except EOFError:
                logger.info("[PIPE] 客户端连接断开 → 自动中止发送任务")
                state.abort_event.set()
                return
            except OSError:
                state.abort_event.set()
                return
    except Exception:
        state.abort_event.set()


def handle_connection(conn, dll, state: SharedState, cfg: dict, stop_event: threading.Event):
    """处理单个管道客户端连接"""
    try:
        msg = conn.recv()

        if not isinstance(msg, dict):
            conn.send({"status": "ERROR", "message": "消息格式错误，期望 dict"})
            return

        action = msg.get("action", "run")

        # ---- status ----
        if action == "status":
            conn.send({
                "status": "running",
                "device_type": cfg["dev_type"],
                "baud_rate": cfg["baud_rate"],
                "can_idx": cfg["can_idx"],
                "channel": f"CAN{cfg['can_idx'] + 1}",
                "log_file": cfg["log_file"],
                "uptime_s": round(time.time() - cfg["start_time"], 1),
            })
            return

        # ---- abort (从第二个客户端发起) ----
        if action == "abort":
            state.abort_event.set()
            logger.info("[PIPE] 收到外部中止请求")
            conn.send({"status": "ABORT_REQUESTED", "message": "已下发中止指令"})
            return

        # ---- run ----
        if action == "run":
            # 防止多个 run 任务并发
            acquired = state.run_lock.acquire(blocking=False)
            if not acquired:
                conn.send({"status": "ERROR", "message": "另一个发送任务正在执行中，请先中止或等待完成。"})
                return

            try:
                # 启动看门狗线程监控客户端连接
                wd = threading.Thread(
                    target=_watchdog_func,
                    args=(conn, state),
                    name="PipeWatchdog",
                    daemon=True,
                )
                wd.start()

                # 执行发送任务
                result = execute_run_task(dll, state, cfg, msg)

                # 尝试将结果发回客户端 (客户端可能已断开)
                try:
                    conn.send(result)
                except (EOFError, OSError, BrokenPipeError):
                    logger.debug("[PIPE] 客户端已断开，无法回传结果")
            finally:
                state.run_lock.release()
            return

        # ---- 未知操作 ----
        conn.send({"status": "ERROR", "message": f"未知操作: {action}，可用: run / status / abort"})

    except EOFError:
        logger.info("[PIPE] 客户端在读取消息前断开连接")
    except Exception as e:
        logger.error(f"[PIPE] 处理连接时发生错误: {e}")
        try:
            conn.send({"status": "ERROR", "message": str(e)})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================================
# 主入口
# ============================================================================
def main():
    # ---- 命令行参数解析 ----
    parser = argparse.ArgumentParser(
        description="CAN 测试常驻服务 (命名管道版本)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "客户端调用示例:\n"
            "  python can/send_payload.py\n"
            "  按 Ctrl+C 杀掉客户端脚本即可自动中止 CAN 报文发送"
        ),
    )
    parser.add_argument("--dev-type", type=int, default=DEFAULT_DEV_TYPE, help=f"设备类型 (默认 {DEFAULT_DEV_TYPE} = USBCAN2)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD_RATE, help=f"波特率 kbps (默认 {DEFAULT_BAUD_RATE})")
    parser.add_argument("--can-idx", type=int, default=DEFAULT_CAN_IDX, help=f"通道索引 (默认 {DEFAULT_CAN_IDX} = CAN1)")
    # 默认日志文件名: logs/can_bus_YYYY-MM-DD.log
    default_log = os.path.join(DEFAULT_LOG_DIR, f"can_bus_{datetime.now().strftime('%Y-%m-%d')}.log")
    parser.add_argument("--log-file", type=str, default=default_log, help=f"接收报文日志路径 (默认 logs/can_bus_<日期>.log)")
    args = parser.parse_args()

    # ---- 加载 DLL ----
    logger.info("=" * 60)
    logger.info("  CAN 测试常驻服务 (命名管道版本)")
    logger.info("=" * 60)
    logger.info("[启动] 正在加载 my_can_bridge.dll ...")

    try:
        dll = load_bridge_dll()
    except Exception as e:
        logger.error(f"[FATAL] 加载 DLL 失败: {e}")
        sys.exit(1)

    logger.info("[启动] DLL 加载成功。")

    # ---- 初始化物理通道 ----
    logger.info(f"[启动] 正在初始化 CAN 通道 (DevType={args.dev_type}, Baud={args.baud}k, Channel=CAN{args.can_idx + 1}) ...")
    init_ret = dll.InitCanBridge(args.dev_type, args.baud, args.can_idx)
    if init_ret != 1:
        logger.error("[FATAL] InitCanBridge 失败，请检查硬件连接与驱动。")
        sys.exit(1)

    logger.info("[启动] CAN 通道已打开，接收线程就绪！")

    # ---- 共享状态 ----
    state = SharedState()
    stop_event = threading.Event()

    # ---- 启动接收线程 (CAN 总线监听) ----
    rx_thread = threading.Thread(
        target=receiver_thread_func,
        args=(dll, args.can_idx, state, args.log_file, stop_event),
        name="CAN_Receiver",
        daemon=True,
    )
    rx_thread.start()

    # ---- 构建运行时配置 ----
    cfg = {
        "dev_type": args.dev_type,
        "baud_rate": args.baud,
        "can_idx": args.can_idx,
        "log_file": os.path.abspath(args.log_file),
        "start_time": time.time(),
    }

    # ---- 创建命名管道监听器 ----
    listener = Listener(DEFAULT_PIPE_ADDRESS, authkey=PIPE_AUTH_KEY)

    # ---- 优雅退出处理 ----
    def shutdown_handler(signum, frame):
        logger.info("\n[退出] 收到终止信号，正在安全关闭 ...")
        stop_event.set()               # 通知所有工作线程停止
        state.abort_event.set()         # 中止正在进行的发送任务
        try:
            listener.close()            # 关闭管道监听器，打断 accept() 阻塞
        except Exception:
            pass

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # ---- 启动服务 ----
    logger.info(f"[启动] 命名管道已就绪: {DEFAULT_PIPE_ADDRESS}")
    logger.info(f"[启动] 支持操作: run (发送报文), status (查看状态), abort (中止发送)")
    logger.info(f"[启动] 客户端断开连接时，当前发送任务会被自动中止")
    logger.info(f"[启动] 按 Ctrl+C 安全退出。")
    logger.info("-" * 60)

    # ---- 管道监听主循环 (在主线程运行) ----
    try:
        while not stop_event.is_set():
            try:
                conn = listener.accept()
                logger.info("[PIPE] 新客户端已连接")

                # 每个连接在独立线程中处理，使主循环可以继续接受新连接
                t = threading.Thread(
                    target=handle_connection,
                    args=(conn, dll, state, cfg, stop_event),
                    name="PipeHandler",
                    daemon=True,
                )
                t.start()

            except (OSError, AssertionError, EOFError):
                if stop_event.is_set():
                    break
                logger.error("[PIPE] 管道监听器发生错误，正在退出...")
                break
    except KeyboardInterrupt:
        shutdown_handler(None, None)
    except SystemExit:
        pass
    finally:
        # ---- 清理资源 ----
        rx_thread.join(timeout=3)       # 等待接收线程结束
        logger.info("[退出] 正在关闭 CAN 桥接释放物理设备 ...")
        dll.CloseCanBridge(args.dev_type, args.can_idx)
        logger.info("[退出] 桥接与物理通道已安全关闭，再见！")


if __name__ == "__main__":
    main()
