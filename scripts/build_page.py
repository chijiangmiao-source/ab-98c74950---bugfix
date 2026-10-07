#!/usr/bin/env python3
"""构建交付页面静态制品并检查必备要素。

用法：python scripts/build_page.py [输出目录]

从同一份渲染器（app/page.py，运行时页面也用它）生成静态单页
``index.html``，检查页面持续展示所需的要素是否齐全：额度总览、可用额度、
冻结额度、发布阶段、接收方回执。任一缺失以非零退出码失败。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.page import REQUIRED_MARKERS, render_page  # noqa: E402


def main(argv: list[str]) -> int:
    out_dir = argv[1] if len(argv) > 1 else os.path.join(os.getcwd(), "dist")
    os.makedirs(out_dir, exist_ok=True)
    html = render_page(
        {
            "total_quota": 0,
            "used": 0,
            "available": 0,
            "completed_amount": 0,
            "frozen_amount": 0,
            "publications": [],
            "deliveries": [],
        }
    )
    out_path = os.path.join(out_dir, "index.html")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    missing = [marker for marker in REQUIRED_MARKERS if marker not in html]
    if missing:
        print(f"[build] 页面缺少必备要素：{missing}", file=sys.stderr)
        return 1
    size = os.path.getsize(out_path)
    print(f"[build] OK：{out_path}（{size} 字节），已检查要素：{', '.join(REQUIRED_MARKERS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
