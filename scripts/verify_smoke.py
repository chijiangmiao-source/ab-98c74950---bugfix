#!/usr/bin/env python3
"""Compose verify 服务入口。

按顺序/并行执行三类检查，全部通过退出码 0，任一失败非零：

1. 先构建交付页面静态制品并检查必备要素；
2. 对编排中的交付服务做 API/HTTP 冒烟：正常交付、超额拒绝（接收端不新增
   回执）、相同载荷重传幂等、改动摘要/额度冲突；
3. 另起本地子进程实例做真实“接收后断电”演练（硬退出 → 重开 → 按发布
   标识查询冻结回执并收敛 → 重开重试幂等）；
4. 代码测试（pytest）与冒烟并行运行。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.page import REQUIRED_MARKERS  # noqa: E402

BASE_URL = os.environ.get("DELIVERY_URL", "http://delivery:8080")
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(name)


def http(method: str, url: str, body: dict | None = None, timeout: float = 5.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_health(url: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = http("GET", f"{url}/health", timeout=2)
            if status == 200:
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def step_build_page() -> None:
    print("\n== 1) 构建交付页面并检查 ==")
    dist = tempfile.mkdtemp(prefix="page-dist-")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "build_page.py"), dist],
        cwd=ROOT, capture_output=True, text=True,
    )
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
    check("页面构建退出码为 0", proc.returncode == 0)
    index = os.path.join(dist, "index.html")
    check("静态制品 index.html 已生成", os.path.exists(index))
    if os.path.exists(index):
        html = open(index, encoding="utf-8").read()
        check("页面包含全部必备展示要素", all(m in html for m in REQUIRED_MARKERS), ", ".join(REQUIRED_MARKERS))


def step_http_smoke() -> None:
    print(f"\n== 2) 对交付服务 {BASE_URL} 做 HTTP 冒烟 ==")
    if not wait_health(BASE_URL):
        check("交付服务健康检查就绪", False)
        return
    check("交付服务健康检查就绪", True)

    status, page = (200, "")
    try:
        with urllib.request.urlopen(f"{BASE_URL}/", timeout=5) as resp:
            status = resp.status
            page = resp.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        check("交付页面可访问", False, str(exc))
    else:
        check("交付页面可访问", status == 200)
        check("运行页面持续展示额度/阶段/回执要素", all(m in page for m in REQUIRED_MARKERS))

    http("POST", f"{BASE_URL}/internal/reset")

    # 总额度 10：交付消耗 6 → 一份回执，可用额度 4
    status, first = http("POST", f"{BASE_URL}/api/deliveries",
                         {"publication_id": "SMOKE-1", "summary": "2026Q3 匿名统计", "amount": 6})
    check("消耗 6 的交付被受理（200）", status == 200, f"HTTP {status}")
    check("受理后阶段为 completed", first.get("stage") == "completed")
    check("受理后返回一份回执", bool(first.get("receipt_id", "").startswith("RC-")))
    check("受理后可用额度为 4", first.get("state", {}).get("available") == 4)
    receipt = first.get("receipt_id")

    # 已有完成发布后再提交消耗 5（6+5>10）必须拒绝
    status, second = http("POST", f"{BASE_URL}/api/deliveries",
                          {"publication_id": "SMOKE-2", "summary": "另一批匿名统计", "amount": 5})
    check("再消耗 5 被拒绝（402）", status == 402, f"HTTP {status} {second.get('error','')}")

    status, state = http("GET", f"{BASE_URL}/api/state")
    receipt_ids = [d["publication_id"] for d in state.get("deliveries", [])]
    check("拒绝后接收端未新增回执", receipt_ids == ["SMOKE-1"], str(receipt_ids))
    check("拒绝后可用额度仍为 4", state.get("available") == 4)
    check("已完成+冻结之和不超过总额度", state.get("used", 0) <= state.get("total_quota", 0))

    # 同标识相同载荷重传 → 原回执、不再扣额
    status, retry = http("POST", f"{BASE_URL}/api/deliveries",
                         {"publication_id": "SMOKE-1", "summary": "2026Q3 匿名统计", "amount": 6})
    check("相同载荷重传返回 200", status == 200)
    check("重传返回原回执", retry.get("receipt_id") == receipt)
    check("重传不再扣额（可用仍为 4）", retry.get("state", {}).get("available") == 4)
    status, state = http("GET", f"{BASE_URL}/api/state")
    check("重传后仍只有一条发布记录", len(state.get("publications", [])) == 1)
    check("重传后交付次数仍为 1", state["deliveries"][0]["delivered_count"] == 1)

    # 改动摘要或额度 → 冲突
    status, conflict_a = http("POST", f"{BASE_URL}/api/deliveries",
                              {"publication_id": "SMOKE-1", "summary": "被改动的摘要", "amount": 6})
    check("改动摘要返回冲突 409", status == 409, f"HTTP {status}")
    status, conflict_b = http("POST", f"{BASE_URL}/api/deliveries",
                              {"publication_id": "SMOKE-1", "summary": "2026Q3 匿名统计", "amount": 5})
    check("改动额度返回冲突 409", status == 409, f"HTTP {status}")
    status, state = http("GET", f"{BASE_URL}/api/state")
    check("冲突后额度与记录数不变", state.get("available") == 4 and len(state.get("publications", [])) == 1)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_local(port: int, data_file: str, auto_recover: bool) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "PORT": str(port), "HOST": "127.0.0.1", "DATA_FILE": data_file,
        "TOTAL_QUOTA": "10", "AUTO_RECOVER": "1" if auto_recover else "0",
        "PYTHONPATH": ROOT,
    })
    return subprocess.Popen(
        [sys.executable, "-m", "app.server"], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def step_crash_recovery() -> None:
    print("\n== 3) 真实接收后断电恢复演练（子进程硬退出 + 重开） ==")
    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="crash-ledger-")
    data_file = os.path.join(data_dir, "ledger.json")
    url = f"http://127.0.0.1:{port}"

    proc = _start_local(port, data_file, auto_recover=False)
    try:
        check("断电演练实例就绪", wait_health(url))
        crashed = False
        try:
            http("POST", f"{url}/api/deliveries",
                 {"publication_id": "CRASH-1", "summary": "断电载荷", "amount": 6,
                  "crash_after_receive": True}, timeout=5)
        except (urllib.error.URLError, ConnectionError, ConnectionResetError):
            crashed = True
        check("提交后本端在记账完成前退出（连接中断）", crashed)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        check("断电退出码为 99", proc.returncode == 99, f"rc={proc.returncode}")

        on_disk = json.load(open(data_file, encoding="utf-8"))
        check("磁盘现场：本端发布停留在 frozen", on_disk["publications"]["CRASH-1"]["stage"] == "frozen")
        check("磁盘现场：接收端已持久化首份回执", on_disk["deliveries"]["CRASH-1"]["delivered_count"] == 1)
        check("磁盘现场：冻结额占用总额度（可用 4）",
              10 - sum(p["amount"] for p in on_disk["publications"].values()) == 4)
    finally:
        if proc.poll() is None:
            proc.kill()

    # 重开（暂不自动恢复）：frozen 可查询
    proc = _start_local(port, data_file, auto_recover=False)
    try:
        check("断电后重开就绪", wait_health(url))
        status, state = http("GET", f"{url}/api/state")
        check("重开后冻结额度 6、可用额度 4",
              state.get("frozen_amount") == 6 and state.get("available") == 4)
        pub = next((p for p in state["publications"] if p["publication_id"] == "CRASH-1"), None)
        check("重开后发布阶段仍为 frozen（未终结保留额）", bool(pub and pub["stage"] == "frozen"))
        check("重开后接收端交付次数仍为 1", state["deliveries"][0]["delivered_count"] == 1)

        # 按发布标识查询冻结回执并收敛为原发布完成
        status, recovered = http("POST", f"{url}/api/recover", {})
        check("收敛接口返回 200 且收敛 1 个发布",
              status == 200 and len(recovered.get("recovered", [])) == 1)
        check("收敛后阶段为 completed",
              recovered.get("recovered", [{}])[0].get("stage") == "completed")
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    # 再次重开 + 恢复重试：一条记录、一份回执、一次冻结
    proc = _start_local(port, data_file, auto_recover=True)
    try:
        check("收敛后再次重开就绪", wait_health(url))
        status, state = http("GET", f"{url}/api/state")
        check("最终：仅一条发布记录", len(state.get("publications", [])) == 1)
        check("最终：仅一份接收回执且交付次数为 1",
              len(state.get("deliveries", [])) == 1 and state["deliveries"][0]["delivered_count"] == 1)
        check("最终：冻结 0、可用 4",
              state.get("frozen_amount") == 0 and state.get("available") == 4)
        receipt = state["publications"][0]["receipt_id"]

        status, retry = http("POST", f"{url}/api/deliveries",
                             {"publication_id": "CRASH-1", "summary": "断电载荷", "amount": 6})
        check("恢复后同载荷重传返回原回执", status == 200 and retry.get("receipt_id") == receipt)
        check("恢复后重传不再扣额", retry.get("state", {}).get("available") == 4)
        status, state = http("GET", f"{url}/api/state")
        check("恢复后重传仍为一条记录/一份回执/次数 1",
              len(state["publications"]) == 1 and len(state["deliveries"]) == 1
              and state["deliveries"][0]["delivered_count"] == 1)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def main() -> int:
    # 先启动代码测试，使其与页面构建、HTTP 冒烟、断电演练并行
    test_proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", os.path.join(ROOT, "tests")],
        cwd=ROOT,
    )
    try:
        step_build_page()
        step_http_smoke()
        step_crash_recovery()
    finally:
        test_proc.wait()
    test_rc = test_proc.returncode

    print("\n== 汇总 ==")
    print(f"pytest 退出码：{test_rc}")
    if test_rc != 0:
        FAILURES.append("pytest")
    if FAILURES:
        print(f"verify 失败项：{FAILURES}")
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS —— 页面构建、HTTP 冒烟（额度拒绝/幂等/冲突/断电恢复）、代码测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
