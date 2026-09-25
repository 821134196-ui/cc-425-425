#!/usr/bin/env python3
"""单条本地启动命令入口：

    python3 run.py                # 生成虚构样例并在 http://127.0.0.1:8000 启动
    python3 run.py --seed-only    # 仅重置并生成样例
    python3 run.py --port 8080
"""
from parking_review.demo import main

if __name__ == "__main__":
    raise SystemExit(main())
