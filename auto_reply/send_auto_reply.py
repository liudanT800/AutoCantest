"""
自动回复规则下发客户端
======================
通过命名管道向 CAN 测试常驻服务动态下发/停用/查询自动回复规则。
无需重启服务。

用法:
  python send_auto_reply.py          # 下发 auto_reply_rules.py 中的规则
  python send_auto_reply.py --stop   # 停用自动回复
  python send_auto_reply.py --status # 查看当前自动回复状态
"""

import sys
import os
import json
import argparse
from multiprocessing.connection import Client
from AutoCantest.auto_reply.auto_reply_rules_data import RAW_RULES

PIPE_ADDRESS = r'\\.\pipe\cantest_pipe'
AUTH_KEY = b'cantest'

# 动态确保脚本所在目录在 sys.path 中
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# JSON 明文缓存文件路径
CACHE_FILE = os.path.join(script_dir, 'auto_reply_rules_cache.json')

def normalize_raw_rules(raw_input):
    """展开嵌套列表/多行字符串，返回去空白去注释的行列表"""
    if isinstance(raw_input, str):
        return [ln for ln in raw_input.strip().split('\n')]
    elif isinstance(raw_input, (list, tuple)):
        lines = []
        for item in raw_input:
            if isinstance(item, str):
                lines.extend(item.strip().split('\n'))
            elif isinstance(item, (list, tuple)):
                lines.extend(normalize_raw_rules(item))
        return lines
    return []


def parse_rule_line(line):
    """解析单行 raw 规则为服务期望的 dict。
    
    格式: match_id  match_pattern  |  reply_id  reply_data  [delay_ms]
    
    返回:
        {"match_id": str, "match_pattern": str|None, "reply_frames": [{...}]}
        或 None（空行/注释行）
    """
    line = line.strip()
    if not line or line.startswith('#'):
        return None

    # 按 | 分割 match 侧和 reply 侧
    parts = [p.strip() for p in line.split('|')]
    if len(parts) != 2:
        return None

    # --- 解析 match 侧 ---
    match_tokens = parts[0].split()
    if len(match_tokens) < 1:
        return None

    match_id = match_tokens[0]
    if not match_id.lower().startswith('0x'):
        match_id = '0x' + match_id

    # 剩余 token 最多取 8 个组成 match_pattern
    pattern_tokens = match_tokens[1:9]
    if pattern_tokens and pattern_tokens[0].lower() == 'none':
        match_pattern = None
    elif pattern_tokens:
        match_pattern = ' '.join(pattern_tokens)
    else:
        match_pattern = None

    # --- 解析 reply 侧 ---
    reply_tokens = parts[1].split()
    if len(reply_tokens) < 2:
        return None

    reply_id = reply_tokens[0]
    if not reply_id.lower().startswith('0x'):
        reply_id = '0x' + reply_id

    # 取前 8 个字节作为 data
    data_tokens = reply_tokens[1:9]
    reply_data = ' '.join(data_tokens) if data_tokens else '00 00 00 00 00 00 00 00'

    reply_frame = {"id": reply_id, "data": reply_data}

    # 可选 delay_ms（紧跟在 data 之后）
    if len(reply_tokens) > 9:
        try:
            reply_frame["delay_ms"] = int(reply_tokens[9])
        except ValueError:
            pass

    return {
        "match_id": match_id,
        "match_pattern": match_pattern,
        "reply_frames": [reply_frame],
    }

def save_cache(rules):
    """将解析后的规则列表保存为 JSON 明文缓存"""
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"警告: 保存规则缓存失败: {e}")


def load_cache():
    """从 JSON 缓存文件加载规则列表，失败返回 None"""
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"警告: 读取规则缓存失败: {e}")
        return None


# 构建 AUTO_REPLY_RULES
# 优先级: raw 解析 > JSON 缓存
# 1. 先尝试从 RAW_RULES 解析，成功则更新缓存并使用
# 2. raw 解析为空时，回退读取 JSON 缓存
raw_lines = normalize_raw_rules(RAW_RULES)
AUTO_REPLY_RULES = []
for line in raw_lines:
    rule = parse_rule_line(line)
    if rule:
        AUTO_REPLY_RULES.append(rule)

if AUTO_REPLY_RULES:
    # raw 解析成功，更新 JSON 缓存
    save_cache(AUTO_REPLY_RULES)
else:
    # raw 解析为空，尝试从 JSON 缓存加载
    cached = load_cache()
    if cached:
        print(f"已从缓存加载 {len(cached)} 条自动回复规则")
        AUTO_REPLY_RULES = cached
    else:
        print("警告: 未能解析任何规则，且无可用缓存")

def main():
    parser = argparse.ArgumentParser(description="自动回复规则下发客户端")
    parser.add_argument("--stop", action="store_true", help="停用自动回复")
    parser.add_argument("--status", action="store_true", help="查看自动回复状态")
    args = parser.parse_args()

    conn = None
    try:
        conn = Client(PIPE_ADDRESS, authkey=AUTH_KEY)

        if args.stop:
            print("正在停用自动回复...")
            conn.send({"action": "auto_reply_stop"})
        elif args.status:
            print("正在查询自动回复状态...")
            conn.send({"action": "auto_reply_status"})
        else:
            print(f"正在下发 {len(AUTO_REPLY_RULES)} 条自动回复规则...")
            conn.send({"action": "auto_reply_start", "rules": AUTO_REPLY_RULES})

        result = conn.recv()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    except ImportError as e:
        print(f"错误: 无法导入 auto_reply_rules.py: {e}")
        sys.exit(1)
    except FileNotFoundError:
        print(f"错误: 无法连接到服务管道 '{PIPE_ADDRESS}'，请先启动 CAN 测试常驻服务。")
        sys.exit(1)
    except ConnectionRefusedError:
        print(f"错误: 服务管道连接被拒绝，请确认常驻服务已启动。")
        sys.exit(1)
    except EOFError:
        print("错误: 管道连接意外断开。")
        sys.exit(1)
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
