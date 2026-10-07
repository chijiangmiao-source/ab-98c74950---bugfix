"""真实“接收后断电”恢复测试：杀进程 → 重开 → 按标识收敛。

直接以子进程启动 Flask 服务，提交 ``crash_after_receive`` 后进程以
``os._exit`` 硬退出（连接被切断），随后以不同的 AUTO_RECOVER 配置重开，
验证磁盘现场与最终收敛结果。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_health(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.2)
    raise AssertionError(f"服务未在 {timeout}s 内就绪：{last_error}")


def _start(port: int, data_file: str, auto_recover: bool) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        {
            "PORT": str(port),
            "HOST": "127.0.0.1",
            "DATA_FILE": data_file,
            "TOTAL_QUOTA": "10",
            "AUTO_RECOVER": "1" if auto_recover else "0",
            "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        }
    )
    return subprocess.Popen(
        [sys.executable, "-m", "app.server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _post(port: int, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/deliveries",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_crash_after_receive_then_recover_on_restart(tmp_path):
    port = _free_port()
    data_file = str(tmp_path / "ledger.json")

    proc = _start(port, data_file, auto_recover=False)
    try:
        _wait_health(port)

        # 接收后断电：服务在接收端落盘后硬退出，HTTP 连接被切断
        crashed = False
        try:
            _post(port, {"publication_id": "PUB-CRASH", "summary": "断电摘要", "amount": 6, "crash_after_receive": True})
        except (urllib.error.URLError, ConnectionError, ConnectionResetError):
            crashed = True
        assert crashed

        proc.wait(timeout=10)
        assert proc.returncode == 99

        # 磁盘现场：本端 frozen + 接收端持有回执，交付次数 1
        with open(data_file, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        assert on_disk["publications"]["PUB-CRASH"]["stage"] == "frozen"
        assert on_disk["deliveries"]["PUB-CRASH"]["delivered_count"] == 1
        assert on_disk["deliveries"]["PUB-CRASH"]["summary"] == "断电摘要"
    finally:
        if proc.poll() is None:
            proc.kill()

    # 重开（不自动恢复）：frozen 仍可见，额度继续被保留
    proc = _start(port, data_file, auto_recover=False)
    try:
        _wait_health(port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as resp:
            state = json.loads(resp.read())
        assert state["frozen_amount"] == 6
        assert state["available"] == 4
        pub = next(p for p in state["publications"] if p["publication_id"] == "PUB-CRASH")
        assert pub["stage"] == "frozen"

        # 显式按发布标识查询冻结回执并收敛
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/recover", data=b"{}", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            recovered = json.loads(resp.read())
        assert len(recovered["recovered"]) == 1
        assert recovered["recovered"][0]["stage"] == "completed"
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    # 再次重开：恢复重试幂等，仍是一条记录、一份回执、一次冻结
    proc = _start(port, data_file, auto_recover=True)
    try:
        _wait_health(port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as resp:
            state = json.loads(resp.read())
        assert state["available"] == 4
        assert state["frozen_amount"] == 0
        assert len(state["publications"]) == 1
        assert len(state["deliveries"]) == 1
        assert state["deliveries"][0]["delivered_count"] == 1

        # 重传相同载荷：原回执、不再扣额
        status, body = _post(port, {"publication_id": "PUB-CRASH", "summary": "断电摘要", "amount": 6})
        assert status == 200
        assert body["receipt_id"] == state["publications"][0]["receipt_id"]
        assert body["state"]["available"] == 4
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as resp:
            final_state = json.loads(resp.read())
        assert len(final_state["publications"]) == 1
        assert final_state["deliveries"][0]["delivered_count"] == 1
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_two_ids_same_payload_one_crashes_then_restart_recovers(tmp_path):
    """验收：两个标识使用相同摘要和额度，其中一次接收后断电并重启恢复。

    核对：两份发布均完成；接收端有两份对应回执且交付次数各为 1；额度只按
    两次提交扣减；同标识重传幂等、改载荷冲突、第三个同载荷标识超额拒绝。
    """
    port = _free_port()
    data_file = str(tmp_path / "ledger.json")
    summary = "两个标识共用的相同摘要"
    amount = 4

    proc = _start(port, data_file, auto_recover=False)
    try:
        _wait_health(port)
        status, first = _post(port, {"publication_id": "PUB-DUP-A", "summary": summary, "amount": amount})
        assert status == 200 and first["stage"] == "completed"

        # 第二份：相同摘要与额度，但选择“接收后断电”
        crashed = False
        try:
            _post(port, {
                "publication_id": "PUB-DUP-B", "summary": summary, "amount": amount,
                "crash_after_receive": True,
            })
        except (urllib.error.URLError, ConnectionError, ConnectionResetError):
            crashed = True
        assert crashed
        proc.wait(timeout=10)
        assert proc.returncode == 99

        # 磁盘现场：两份发布（A 完成、B 冻结），接收端两份独立回执
        with open(data_file, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        assert on_disk["publications"]["PUB-DUP-A"]["stage"] == "completed"
        assert on_disk["publications"]["PUB-DUP-B"]["stage"] == "frozen"
        assert set(on_disk["deliveries"]) == {"PUB-DUP-A", "PUB-DUP-B"}
        for pub_id in ("PUB-DUP-A", "PUB-DUP-B"):
            d = on_disk["deliveries"][pub_id]
            assert d["delivered_count"] == 1
            assert d["summary"] == summary and d["amount"] == amount
            assert d["receipt_id"] == on_disk["publications"][pub_id]["receipt_id"]
        assert (
            on_disk["deliveries"]["PUB-DUP-A"]["receipt_id"]
            != on_disk["deliveries"]["PUB-DUP-B"]["receipt_id"]
        )
    finally:
        if proc.poll() is None:
            proc.kill()

    # 重开并自动恢复：两份均完成，额度只扣两次（4+4=8，可用 2）
    proc = _start(port, data_file, auto_recover=True)
    try:
        _wait_health(port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as resp:
            state = json.loads(resp.read())
        assert {p["publication_id"]: p["stage"] for p in state["publications"]} == {
            "PUB-DUP-A": "completed", "PUB-DUP-B": "completed",
        }
        assert state["frozen_amount"] == 0
        assert state["used"] == 8 and state["available"] == 2
        by_id = {d["publication_id"]: d for d in state["deliveries"]}
        assert set(by_id) == {"PUB-DUP-A", "PUB-DUP-B"}
        for d in by_id.values():
            assert d["delivered_count"] == 1
            assert d["summary"] == summary and d["amount"] == amount
        receipts_a = {d["receipt_id"] for d in state["deliveries"]}
        assert len(receipts_a) == 2

        # 同标识同载荷重传：原回执、不再扣额
        status, retry = _post(port, {"publication_id": "PUB-DUP-B", "summary": summary, "amount": amount})
        assert status == 200
        assert retry["receipt_id"] == by_id["PUB-DUP-B"]["receipt_id"]
        assert retry["state"]["available"] == 2

        # 改摘要/改额度：冲突
        assert _post(port, {"publication_id": "PUB-DUP-B", "summary": "改过的摘要", "amount": amount})[0] == 409
        assert _post(port, {"publication_id": "PUB-DUP-B", "summary": summary, "amount": 3})[0] == 409

        # 第三个同载荷新标识：8+4>10，超额拒绝且接收端不新增回执
        status, _ = _post(port, {"publication_id": "PUB-DUP-C", "summary": summary, "amount": amount})
        assert status == 402
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    # 再次重开：稳定收敛，仍为两份发布/两份回执/次数各 1
    proc = _start(port, data_file, auto_recover=True)
    try:
        _wait_health(port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as resp:
            final_state = json.loads(resp.read())
        assert len(final_state["publications"]) == 2
        assert len(final_state["deliveries"]) == 2
        assert all(d["delivered_count"] == 1 for d in final_state["deliveries"])
        assert final_state["frozen_amount"] == 0
        assert final_state["available"] == 2
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_auto_recover_on_boot(tmp_path):
    port = _free_port()
    data_file = str(tmp_path / "ledger.json")

    proc = _start(port, data_file, auto_recover=False)
    try:
        _wait_health(port)
        try:
            _post(port, {"publication_id": "PUB-AUTO", "summary": "开机恢复", "amount": 3, "crash_after_receive": True})
        except (urllib.error.URLError, ConnectionError, ConnectionResetError):
            pass
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    proc = _start(port, data_file, auto_recover=True)
    try:
        _wait_health(port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as resp:
            state = json.loads(resp.read())
        pub = next(p for p in state["publications"] if p["publication_id"] == "PUB-AUTO")
        assert pub["stage"] == "completed"
        assert state["frozen_amount"] == 0
        assert state["available"] == 7
        assert len(state["deliveries"]) == 1
        assert state["deliveries"][0]["delivered_count"] == 1
    finally:
        proc.terminate()
        proc.wait(timeout=10)
