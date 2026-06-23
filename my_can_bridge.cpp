#include <windows.h>
#include <iostream>
#include <string>
#include <vector>
#include <queue>
#include <atomic>
#include <cstring>
#include <cstdio>
#include "include/ControlCAN.h"

// ============================================================================
// 全局状态和 Win32 线程/同步原语 (支持双通道独立控制)
// ============================================================================
static HANDLE g_hReceiveThread[2] = { NULL, NULL };
static DWORD g_dwThreadId[2] = { 0, 0 };
static CRITICAL_SECTION g_queueCriticalSection[2];

static std::queue<std::string> g_receiveQueue[2];
static std::atomic<bool> g_threadRunning[2] = { false, false };
static std::atomic<bool> g_channelOpened[2] = { false, false };
static std::atomic<bool> g_deviceOpened(false);

static std::vector<unsigned int> g_filterIDs[2];

// 线程局部存储，用于安全返回 const char* 指针，避免多线程访问冲突与内存泄露
static thread_local std::string g_lastMessage;

// ============================================================================
// C++ 静态全局生命周期管理类 (确保 EXE 和 DLL 运行下都能正确初始化/析构同步原语)
// ============================================================================
struct BridgeLifetimeManager {
    BridgeLifetimeManager() {
        InitializeCriticalSection(&g_queueCriticalSection[0]);
        InitializeCriticalSection(&g_queueCriticalSection[1]);
    }
    ~BridgeLifetimeManager() {
        // 自动停止接收线程，防止内存泄露和进程崩溃
        for (int i = 0; i < 2; ++i) {
            g_threadRunning[i] = false;
            if (g_hReceiveThread[i] != NULL) {
                WaitForSingleObject(g_hReceiveThread[i], 2000);
                CloseHandle(g_hReceiveThread[i]);
                g_hReceiveThread[i] = NULL;
            }
        }
        DeleteCriticalSection(&g_queueCriticalSection[0]);
        DeleteCriticalSection(&g_queueCriticalSection[1]);
    }
};
static BridgeLifetimeManager g_lifetimeManager;

// ============================================================================
// 内部辅助函数
// ============================================================================

// 将十六进制字符串解析为 8 字节数组（支持 "10FFFFFFFFFFFFFF" 或 "10 FF FF..." 等格式）
static bool ParseHexStr(const char* hex_str, uint8_t* out_data) {
    if (!hex_str) return false;
    
    // 默认数据清零
    std::memset(out_data, 0, 8);
    
    // 清理字符串，提取有效的十六进制字符
    std::vector<char> clean_chars;
    for (int i = 0; hex_str[i] != '\0'; ++i) {
        char c = hex_str[i];
        if ((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
            clean_chars.push_back(c);
        }
    }
    
    int len = clean_chars.size();
    if (len == 0) return false;
    
    // 十六进制字符转数值的 lambda 函数
    auto hex_val = [](char c) -> uint8_t {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return 10 + (c - 'a');
        if (c >= 'A' && c <= 'F') return 10 + (c - 'A');
        return 0;
    };
    
    // 最多转换 8 个字节
    int byte_count = len / 2;
    if (byte_count > 8) byte_count = 8;
    
    for (int i = 0; i < byte_count; ++i) {
        uint8_t high = hex_val(clean_chars[i * 2]);
        uint8_t low = hex_val(clean_chars[i * 2 + 1]);
        out_data[i] = (high << 4) | low;
    }
    
    return true;
}

// 接收线程的主循环，轮询接收 CAN 报文 (符合 Win32 ThreadProc 签名)
static DWORD WINAPI ReceiveThreadProc(LPVOID lpParam) {
    // 解析参数：低16位为设备类型，高16位为通道索引
    int val = (int)(intptr_t)lpParam;
    int dev_type = val & 0xFFFF;
    int can_idx = (val >> 16) & 0xFFFF;
    
    if (can_idx < 0 || can_idx > 1) return 0;
    
    VCI_CAN_OBJ rx_objs[200]; // 每次最大接收 200 帧
    
    while (g_threadRunning[can_idx]) {
        // 调用 VCI_Receive 接收数据，超时时间设为 50ms 稀释 CPU 占用
        ULONG rx_num = VCI_Receive(dev_type, 0, can_idx, rx_objs, 200, 50);
        if (rx_num > 0) {
            EnterCriticalSection(&g_queueCriticalSection[can_idx]);
            for (ULONG i = 0; i < rx_num; ++i) {
                // 软件过滤逻辑
                if (!g_filterIDs[can_idx].empty()) {
                    bool matched = false;
                    for (size_t f = 0; f < g_filterIDs[can_idx].size(); ++f) {
                        if (rx_objs[i].ID == g_filterIDs[can_idx][f]) {
                            matched = true;
                            break;
                        }
                    }
                    if (!matched) continue;
                }
                
                // 格式化报文：例如 "CAN0|ID:0x2A6|Data:10 FF FF FF FF FF FF FF"
                char id_buf[64];
                std::sprintf(id_buf, "CAN%d|ID:0x%X|Data:", can_idx, rx_objs[i].ID);
                
                std::string msg = id_buf;
                for (int j = 0; j < rx_objs[i].DataLen; ++j) {
                    char byte_buf[8];
                    if (j == rx_objs[i].DataLen - 1) {
                        std::sprintf(byte_buf, "%02X", rx_objs[i].Data[j]);
                    } else {
                        std::sprintf(byte_buf, "%02X ", rx_objs[i].Data[j]);
                    }
                    msg += byte_buf;
                }
                
                // 队列限长（例如最大1万条），防止总线流量过大而堆积爆内存
                if (g_receiveQueue[can_idx].size() >= 10000) {
                    g_receiveQueue[can_idx].pop();
                }
                g_receiveQueue[can_idx].push(msg);
            }
            LeaveCriticalSection(&g_queueCriticalSection[can_idx]);
        } else {
            // 没有报文时，短暂 Sleep 以稀释 CPU 占用
            Sleep(5);
        }
    }
    return 0;
}

// ============================================================================
// 导出接口定义 (C 风格, __stdcall 调用约定)
// ============================================================================

extern "C" {

// 1. 初始化并启动指定的 CAN 通道
__declspec(dllexport) int __stdcall InitCanBridge(int dev_type, int baud_rate, int can_idx) {
    if (can_idx < 0 || can_idx > 1) {
        return 0; // 无效通道
    }
    
    if (g_channelOpened[can_idx]) {
        return 1; // 该通道已经打开
    }
    
    // 打开设备 (固定操作第 0 个设备，如果设备没有打开则打开)
    if (!g_deviceOpened) {
        DWORD res = VCI_OpenDevice(dev_type, 0, 0);
        if (res != STATUS_OK) {
            return 0;
        }
        g_deviceOpened = true;
    }
    
    // 配置初始化结构体
    VCI_INIT_CONFIG config;
    config.AccCode = 0x0;
    config.AccMask = 0xFFFFFFFF; // 验收屏蔽码，全放行
    config.Reserved = 0;
    config.Filter = 1;           // 单滤波模式
    config.Mode = 0;             // 正常工作模式
    
    // 波特率参数映射
    if (baud_rate == 125) {
        config.Timing0 = 0x03;
        config.Timing1 = 0x1C;
    } else if (baud_rate == 250) {
        config.Timing0 = 0x01;
        config.Timing1 = 0x1C;
    } else if (baud_rate == 500) {
        config.Timing0 = 0x00;
        config.Timing1 = 0x1C;
    } else if (baud_rate == 1000) {
        config.Timing0 = 0x00;
        config.Timing1 = 0x14;
    } else {
        // 其他波特率默认使用 500k 参数
        config.Timing0 = 0x00;
        config.Timing1 = 0x1C;
    }
    
    // 初始化通道
    DWORD res = VCI_InitCAN(dev_type, 0, can_idx, &config);
    if (res != STATUS_OK) {
        // 如果另一个通道也没打开，则关闭设备
        bool otherOpened = false;
        for (int i = 0; i < 2; ++i) {
            if (i != can_idx && g_channelOpened[i]) otherOpened = true;
        }
        if (!otherOpened) {
            VCI_CloseDevice(dev_type, 0);
            g_deviceOpened = false;
        }
        return 0;
    }
    
    // 启动通道
    res = VCI_StartCAN(dev_type, 0, can_idx);
    if (res != STATUS_OK) {
        bool otherOpened = false;
        for (int i = 0; i < 2; ++i) {
            if (i != can_idx && g_channelOpened[i]) otherOpened = true;
        }
        if (!otherOpened) {
            VCI_CloseDevice(dev_type, 0);
            g_deviceOpened = false;
        }
        return 0;
    }
    
    g_channelOpened[can_idx] = true;
    
    // 开启该通道的独立接收线程
    g_threadRunning[can_idx] = true;
    int threadParam = (can_idx << 16) | (dev_type & 0xFFFF);
    g_hReceiveThread[can_idx] = CreateThread(
        NULL,
        0,
        ReceiveThreadProc,
        (LPVOID)(intptr_t)threadParam,
        0,
        &g_dwThreadId[can_idx]
    );
    
    if (g_hReceiveThread[can_idx] == NULL) {
        VCI_ResetCAN(dev_type, 0, can_idx);
        g_channelOpened[can_idx] = false;
        g_threadRunning[can_idx] = false;
        
        bool otherOpened = false;
        for (int i = 0; i < 2; ++i) {
            if (i != can_idx && g_channelOpened[i]) otherOpened = true;
        }
        if (!otherOpened) {
            VCI_CloseDevice(dev_type, 0);
            g_deviceOpened = false;
        }
        return 0;
    }
    
    return 1;
}

// 2. 在指定通道发送一帧 8 字节的标准数据帧
__declspec(dllexport) int __stdcall SendCanHex(int dev_type, unsigned int id, const char* hex_str, int can_idx) {
    if (can_idx < 0 || can_idx > 1 || !g_channelOpened[can_idx]) {
        return 0; // 该通道未打开
    }
    
    VCI_CAN_OBJ obj;
    std::memset(&obj, 0, sizeof(VCI_CAN_OBJ));
    obj.ID = id;
    obj.SendType = 0;     // 正常发送
    obj.RemoteFlag = 0;   // 数据帧
    obj.ExternFlag = (id > 0x7FF) ? 1 : 0;   // 自动根据 ID 大小判断标准帧/扩展帧
    obj.DataLen = 8;      // 报文长度固定为 8 字节
    
    uint8_t parsed_data[8];
    if (!ParseHexStr(hex_str, parsed_data)) {
        return 0; // 解析 Hex 字符串失败
    }
    std::memcpy(obj.Data, parsed_data, 8);
    
    // 传输报文
    ULONG tx_res = VCI_Transmit(dev_type, 0, can_idx, &obj, 1);
    return (tx_res == 1) ? 1 : 0;
}

// 3. 提取指定通道接收队列中的最老报文
__declspec(dllexport) const char* __stdcall FetchReceivedMessage(int can_idx) {
    if (can_idx < 0 || can_idx > 1) {
        return nullptr;
    }
    
    EnterCriticalSection(&g_queueCriticalSection[can_idx]);
    if (g_receiveQueue[can_idx].empty()) {
        LeaveCriticalSection(&g_queueCriticalSection[can_idx]);
        return nullptr;
    }
    
    // 提取并移出队列
    g_lastMessage = g_receiveQueue[can_idx].front();
    g_receiveQueue[can_idx].pop();
    
    LeaveCriticalSection(&g_queueCriticalSection[can_idx]);
    return g_lastMessage.c_str();
}

// 5. 设置通道过滤器 (软件过滤)
__declspec(dllexport) void __stdcall SetChannelFilter(int can_idx, const unsigned int* ids, int count) {
    if (can_idx < 0 || can_idx > 1) return;
    
    EnterCriticalSection(&g_queueCriticalSection[can_idx]);
    g_filterIDs[can_idx].clear();
    if (ids && count > 0) {
        for (int i = 0; i < count; ++i) {
            g_filterIDs[can_idx].push_back(ids[i]);
        }
    }
    LeaveCriticalSection(&g_queueCriticalSection[can_idx]);
}

// 额外导出：关闭指定的 CAN 桥接通道
__declspec(dllexport) int __stdcall CloseCanBridge(int dev_type, int can_idx) {
    if (can_idx < 0 || can_idx > 1 || !g_channelOpened[can_idx]) {
        return 1;
    }
    
    g_threadRunning[can_idx] = false;
    if (g_hReceiveThread[can_idx] != NULL) {
        // 等待线程结束，限时 2 秒
        WaitForSingleObject(g_hReceiveThread[can_idx], 2000);
        CloseHandle(g_hReceiveThread[can_idx]);
        g_hReceiveThread[can_idx] = NULL;
    }
    
    VCI_ResetCAN(dev_type, 0, can_idx);
    g_channelOpened[can_idx] = false;
    
    // 清理队列
    EnterCriticalSection(&g_queueCriticalSection[can_idx]);
    while (!g_receiveQueue[can_idx].empty()) {
        g_receiveQueue[can_idx].pop();
    }
    LeaveCriticalSection(&g_queueCriticalSection[can_idx]);
    
    // 如果所有通道均已关闭且设备打开，则关闭设备
    bool otherOpened = false;
    for (int i = 0; i < 2; ++i) {
        if (g_channelOpened[i]) otherOpened = true;
    }
    if (!otherOpened && g_deviceOpened) {
        VCI_CloseDevice(dev_type, 0);
        g_deviceOpened = false;
    }
    
    return 1;
}

} // extern "C"

// ============================================================================
// DllMain 仅保留供动态库环境使用，可与全局静态析构配合
// ============================================================================
BOOL APIENTRY DllMain(HMODULE hModule, DWORD ul_reason_for_call, LPVOID lpReserved) {
    switch (ul_reason_for_call) {
        case DLL_PROCESS_ATTACH:
            break;
        case DLL_THREAD_ATTACH:
            break;
        case DLL_THREAD_DETACH:
            break;
        case DLL_PROCESS_DETACH:
            break;
    }
    return TRUE;
}

// ============================================================================
// 单文件调试/测试入口
// ============================================================================
#if defined(TEST) || defined(LIU_DAN)
int main() {
    std::cout << "================ CAN BRIDGE 单文件测试模式 ================" << std::endl;
    
    int dev_type = 4; // USBCAN2 设备类型
    int baud_rate = 250; // 波特率
    int can_idx = 0; // 可选的通道索引 (0 为 CAN1, 1 为 CAN2)
    
    std::cout << "[INFO] 正在初始化并开启 CAN 桥接 (通道 " << can_idx << ")..." << std::endl;
    int initRes = InitCanBridge(dev_type, baud_rate, can_idx);
    if (initRes != 1) {
        std::cout << "[提示] InitCanBridge 返回失败 (" << initRes << ")，可能未连接实际的 USB-CAN 设备。" << std::endl;
    } else {
        std::cout << "[INFO] InitCanBridge 开启成功！" << std::endl;
        
        // 测试发送报文
        std::cout << "[INFO] 尝试发送测试报文 (ID:0x123)..." << std::endl;
        int txRes = SendCanHex(dev_type, 0x123, "10 FF FF FF FF FF FF FF", can_idx);
        std::cout << "[INFO] SendCanHex 返回值: " << txRes << std::endl;
        
        // 睡眠一会，给接收线程留出响应时间
        std::cout << "[INFO] 睡眠 2000ms 等待接收队列接收..." << std::endl;
        Sleep(2000);
        
        // 测试提取接收到的报文
        std::cout << "[步骤 4] 提取接收报文队列..." << std::endl;
        while (true) {
            const char* msg = FetchReceivedMessage(can_idx);
            if (!msg) {
                std::cout << "  队列当前为空。" << std::endl;
                break;
            }
            std::cout << "  提取报文: " << msg << std::endl;
        }
        
        // 关闭通道
        std::cout << "[步骤 5] 正在关闭 CAN 桥接..." << std::endl;
        CloseCanBridge(dev_type, can_idx);
        std::cout << "[成功] 通道已安全关闭。" << std::endl;
    }
    
    std::cout << "================ 测试完成 ================" << std::endl;
    return 0;
}
#endif
