# CAN Bridge DLL 使用指南 & Python 调用示例

`my_can_bridge.dll` 封装了周立功官方复杂的 C 语言结构体，为高层脚本（如 Python）暴露了三个极简的 C 风格接口，支持双通道（CAN1/CAN2）独立高并发控制。

---

## 1. 暴露的 C 接口定义

在 C++ 中，这 4 个接口全部使用 `extern "C" __stdcall` 导出，且编译时已去除名称修饰（Undecorated），可以直接在 Python 中按名称调用：

```cpp
// 1. 初始化并启动指定的 CAN 通道
// 返回值: 1 为成功，0 为失败
// 参数:
//   - dev_type: 设备类型 (4 代表 USBCAN2)
//   - baud_rate: 波特率 (支持 125, 250, 500, 1000)
//   - can_idx: 通道索引 (0 代表 CAN1, 1 代表 CAN2)
int __stdcall InitCanBridge(int dev_type, int baud_rate, int can_idx);

// 2. 在指定通道发送一帧 8 字节的标准数据帧
// 返回值: 1 为成功，0 为失败
// 参数:
//   - dev_type: 设备类型 (4 代表 USBCAN2)
//   - id: 报文 ID (标准帧范围 0x0 ~ 0x7FF)
//   - hex_str: 报文十六进制内容 (支持带空格，如 "10 FF FF FF FF FF FF FF" 或 "10FFFFFFFFFFFFFF")
//   - can_idx: 通道索引 (0 代表 CAN1, 1 代表 CAN2)
int __stdcall SendCanHex(int dev_type, unsigned int id, const char* hex_str, int can_idx);

// 3. 提取指定通道接收队列中的最老报文 (非阻塞)
// 返回值: 成功则返回格式化好的报文文本 (只读)，队列为空则返回空指针 (None/nullptr)
// 格式如: "CAN0|ID:0x2A6|Data:10 FF FF FF FF FF FF FF"
// 参数:
//   - can_idx: 通道索引 (0 代表 CAN1, 1 代表 CAN2)
const char* __stdcall FetchReceivedMessage(int can_idx);

// 4. 关闭指定的 CAN 桥接通道，释放接收线程与设备资源
// 返回值: 1 为成功
// 参数:
//   - dev_type: 设备类型 (4 代表 USBCAN2)
//   - can_idx: 通道索引 (0 代表 CAN1, 1 代表 CAN2)
int __stdcall CloseCanBridge(int dev_type, int can_idx);

// 5. 设置通道过滤器 (软件过滤)
// 参数:
//   - can_idx: 通道索引 (0 代表 CAN1, 1 代表 CAN2)
//   - ids: 过滤 ID 数组指针
//   - count: 过滤 ID 的数量 (若为 0，则清除过滤器并接收全部报文)
void __stdcall SetChannelFilter(int can_idx, const unsigned int* ids, int count);
```

---

## 2. 参数选择与取值范围说明

调用接口时，各参数的有效可选值及对应硬件配置如下：

### 1) `dev_type` (设备类型)
根据您所连接的周立功或兼容 USB-CAN 适配器的硬件型号传入对应的整数：
- **`3`** : 代表 `VCI_USBCAN1`（单通道 USBCAN-I 适配器）
- **`4`** : 代表 `VCI_USBCAN2` / `VCI_USBCAN2A`（双通道 USBCAN-II 适配器，最常使用的默认设备）
- **`20`**: 代表 `VCI_USBCAN_E_U`（USB-CAN/E-U 增强型单通道适配器）
- **`21`**: 代表 `VCI_USBCAN_2E_U`（USB-CAN/2E-U 增强型双通道适配器）

### 2) `baud_rate` (波特率，单位：kbps)
指定 CAN 总线的传输波特率，内部会自动转换为定时器参数寄存器值（Timing0/Timing1）：
- **`125`** : 125 kbps (低速 CAN)
- **`250`** : 250 kbps
- **`500`** : 500 kbps (标准总线常用默认速度)
- **`1000`**: 1000 kbps / 1 Mbps (高速 CAN)

### 3) `can_idx` (通道索引)
选择操作设备盒上的哪一路物理通道接口：
- **`0`**: CAN1 通道（接线柱上的第一路）
- **`1`**: CAN2 通道（接线柱上的第二路，仅双通道设备支持）

### 4) `filter_ids` (筛选过滤 ID 列表，选填)
在 `run_test.py` 的任务 JSON 中，可以通过该字段指定一个感兴趣的 ID 数组（支持十六进制和十进制）：
- **示例**: `"filter_ids": ["0x18070190", "0x123"]`
- **作用**: 设定后，底层 C++ 线程会在最快时效内把其他不相干的背景噪音报文过滤并丢弃，仅把符合的报文推入接收队列，保护 Python 内存并提升运行效率。

### 5) `expect` (预期报文比对规则，选填)
在 `run_test.py` 的任务 JSON 中，用于定义用例执行的预期结果判断契约：
- **`"id"`**: 期待收到的总线响应报文 ID (如 `"0x18070190"`)。
- **`"data_pattern"`**: 期待的数据 Payload 匹配模式 (支持空格，并且支持用 `**` 或 `*` 代表通配符，如 `"01 07 10 80 65 01 ** **"`)。
- **`"timeout_ms"`**: 接收判定超时上限 (单位毫秒，默认 `2000`)。如果在发送任务中或发送完毕后的超时时限内捕获到了该报文，测试报告显示 `PASS`；否则显示 `FAIL`。

---

## 3. Python (ctypes) 调用模版

请将 `my_can_bridge.dll` 和周立功官方的 `ControlCAN.dll` 放置于 Python 脚本所在的同级目录中。

```python
import ctypes
import time
import os

# 1. 加载 DLL (Windows 64位环境建议使用 WinDLL 或 CDLL 均可)
dll_path = os.path.abspath("my_can_bridge.dll")
can_dll = ctypes.WinDLL(dll_path)

# 2. 声明 C 接口的参数类型和返回值类型
can_dll.InitCanBridge.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
can_dll.InitCanBridge.restype = ctypes.c_int

can_dll.SendCanHex.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_char_p, ctypes.c_int]
can_dll.SendCanHex.restype = ctypes.c_int

can_dll.FetchReceivedMessage.argtypes = [ctypes.c_int]
can_dll.FetchReceivedMessage.restype = ctypes.c_char_p  # 返回类型设为 char*，ctypes 会自动解码为 str 或 bytes

can_dll.CloseCanBridge.argtypes = [ctypes.c_int, ctypes.c_int]
can_dll.CloseCanBridge.restype = ctypes.c_int

# ==========================================
# 3. 业务调用示例
# ==========================================
DEV_USBCAN2 = 4  # 设备类型
BAUD_500K = 500  # 500k 波特率
CAN1 = 0         # 通道 0

print("===> [1] 初始化通道 CAN1...")
ret = can_dll.InitCanBridge(DEV_USBCAN2, BAUD_500K, CAN1)
if ret == 1:
    print("[成功] CAN1 通道启动成功并开启异步接收线程！")
    
    # 4. 发送报文测试
    print("\n===> [2] 发送一帧数据...")
    hex_data = b"10 FF FF FF FF FF FF FF" # 传入 bytes 或用 .encode() 转换为 char* 兼容类型
    tx_ret = can_dll.SendCanHex(DEV_USBCAN2, 0x123, hex_data, CAN1)
    print(f"[发送] SendCanHex 返回值: {tx_ret}")
    
    # 5. 循环提取异步接收数据
    print("\n===> [3] 开始接收数据 (监听 3 秒)...")
    end_time = time.time() + 3.0
    while time.time() < end_time:
        msg = can_dll.FetchReceivedMessage(CAN1)
        if msg:
            # 成功提取报文，msg 已经是 python str 类型
            print(f"  [接收] {msg.decode('utf-8')}")
        else:
            time.sleep(0.01)  # 队列空时短暂休眠，避免抢占 CPU
            
    # 6. 关闭通道
    print("\n===> [4] 关闭通道释放资源...")
    can_dll.CloseCanBridge(DEV_USBCAN2, CAN1)
    print("[成功] 桥接已安全关闭。")
else:
    print("[失败] 初始化失败，请检查 USB 设备物理连接及驱动安装！")
```

---

## 4. 编译参数调整说明

若您需要自行重新编译 DLL，请在 MinGW 终端中使用以下命令。
*注：`"-Wl,--kill-at"` 选项用于剥离 `@符号`，使 Python 能够直接通过函数名调用；`-static-libgcc -static-libstdc++` 选项用于静态链接 GCC 运行时，防止 Python 报“找不到模块依赖项”的错误。*

```powershell
# 编译命令
D:\mingw64\bin\g++.exe -shared my_can_bridge.cpp lib\ControlCAN.lib -o bin\my_can_bridge.dll -std=c++17 -O2 -Wall -static-libgcc -static-libstdc++ "-Wl,--kill-at"
```
