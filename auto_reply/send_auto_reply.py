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

PIPE_ADDRESS = r'\\.\pipe\cantest_pipe'
AUTH_KEY = b'cantest'

# 动态确保脚本所在目录在 sys.path 中
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)


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
            from auto_reply_rules import AUTO_REPLY_RULES

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
