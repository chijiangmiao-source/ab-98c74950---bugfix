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


def test_two_publications_same_payload_one_crashes_then_restart_recovers(tmp_path):
    """验收：两个标识相同摘要+额度，其一接收后断电，重开后各自独立收敛。

    隔离持久化数据中核对：两份发布均完成、接收端有两份对应回执且交付次数
    各为 1、额度只按两次提交扣减；恢复不得复用另一标识的接收证据。
    """
    port = _free_port()
    data_file = str(tmp_path / "ledger.json")
    summary = "两份发布共用的完全相同匿名摘要"
    amount = 3

    proc = _start(port, data_file, auto_recover=False)
    try:
        _wait_health(port)

        # 第一份交付正常完成
        status, first = _post(port, {"publication_id": "PUB-DUP-1", "summary": summary, "amount": amount})
        assert status == 200
        assert first["stage"] == "completed"
        receipt_1 = first["receipt_id"]

        # 第二份以另一稳定标识提交相同载荷，但选择“接收后断电”
        crashed = False
        try:
            _post(port, {
                "publication_id": "PUB-DUP-2", "summary": summary, "amount": amount,
                "crash_after_receive": True,
            })
        except (urllib.error.URLError, ConnectionError, ConnectionResetError):
            crashed = True
        assert crashed
        proc.wait(timeout=10)
        assert proc.returncode == 99

        # 磁盘现场：两个标识各自持有接收记录（不能只有首份）
        with open(data_file, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        assert on_disk["publications"]["PUB-DUP-1"]["stage"] == "completed"
        assert on_disk["publications"]["PUB-DUP-2"]["stage"] == "frozen"
        assert set(on_disk["deliveries"]) == {"PUB-DUP-1", "PUB-DUP-2"}
        receipts_on_disk = {k: d["receipt_id"] for k, d in on_disk["deliveries"].items()}
        assert receipts_on_disk["PUB-DUP-1"] == receipt_1
        assert receipts_on_disk["PUB-DUP-2"] != receipt_1
        assert all(d["delivered_count"] == 1 for d in on_disk["deliveries"].values())
    finally:
        if proc.poll() is None:
            proc.kill()

    # 重开并自动恢复：第二份按自身标识收敛，第一份不受影响
    proc = _start(port, data_file, auto_recover=True)
    try:
        _wait_health(port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as resp:
            state = json.loads(resp.read())

        assert {p["publication_id"]: p["stage"] for p in state["publications"]} == {
            "PUB-DUP-1": "completed", "PUB-DUP-2": "completed",
        }
        # 两份各自的回执，交付次数各为 1
        assert [(d["publication_id"], d["receipt_id"], d["delivered_count"])
                for d in state["deliveries"]] == [
            ("PUB-DUP-1", receipts_on_disk["PUB-DUP-1"], 1),
            ("PUB-DUP-2", receipts_on_disk["PUB-DUP-2"], 1),
        ]
        # 额度只按两次提交扣减：3 + 3 = 6
        assert state["used"] == 6
        assert state["frozen_amount"] == 0
        assert state["available"] == 4

        # 各自同载荷重传仍返回原回执、不再扣额、交付次数不增加
        for pub_id, receipt in (
            ("PUB-DUP-1", receipts_on_disk["PUB-DUP-1"]),
            ("PUB-DUP-2", receipts_on_disk["PUB-DUP-2"]),
        ):
            status, retry = _post(port, {"publication_id": pub_id, "summary": summary, "amount": amount})
            assert status == 200
            assert retry["receipt_id"] == receipt
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as resp:
            final_state = json.loads(resp.read())
        assert len(final_state["publications"]) == 2
        assert len(final_state["deliveries"]) == 2
        assert all(d["delivered_count"] == 1 for d in final_state["deliveries"])
        assert final_state["available"] == 4
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_legacy_affected_ledger_converges_on_reopen(tmp_path):
    """旧版本受影响的持久化账本再次打开：补建缺失回执并收敛，不遗留冻结。"""
    port = _free_port()
    data_file = str(tmp_path / "ledger.json")
    summary = "旧账本里两份标识共用的摘要"

    import hashlib

    def fp(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def rc(pub_id: str) -> str:
        return "RC-" + hashlib.sha256(f"receipt:{pub_id}".encode("utf-8")).hexdigest()[:16]

    shared = {
        "publication_id": "PUB-A", "summary": summary, "summary_fingerprint": fp(summary),
        "amount": 3, "receipt_id": rc("PUB-A"), "delivered_count": 1, "accepted_at": 1000.0,
    }
    legacy = {
        "version": 1,
        "publications": {
            "PUB-A": {
                "publication_id": "PUB-A", "summary": summary, "summary_fingerprint": fp(summary),
                "amount": 3, "stage": "completed", "receipt_id": rc("PUB-A"),
                "frozen_at": 1000.0, "completed_at": 1001.0,
            },
            # 旧版本相同载荷第二份“接收后断电”：接收端无本标识记录 → 永久 frozen
            "PUB-B": {
                "publication_id": "PUB-B", "summary": summary, "summary_fingerprint": fp(summary),
                "amount": 3, "stage": "frozen", "receipt_id": rc("PUB-B"),
                "frozen_at": 1002.0, "completed_at": None,
            },
        },
        "deliveries": {"PUB-A": shared},
        "delivery_payloads": {f"{fp(summary)}:3": dict(shared)},
    }
    with open(data_file, "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)

    proc = _start(port, data_file, auto_recover=True)
    try:
        _wait_health(port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as resp:
            state = json.loads(resp.read())

        stages = {p["publication_id"]: p["stage"] for p in state["publications"]}
        assert stages == {"PUB-A": "completed", "PUB-B": "completed"}
        assert [(d["publication_id"], d["receipt_id"], d["delivered_count"])
                for d in state["deliveries"]] == [
            ("PUB-A", rc("PUB-A"), 1),
            ("PUB-B", rc("PUB-B"), 1),
        ]
        assert state["frozen_amount"] == 0
        assert state["available"] == 4

        # 旧载荷索引不应残留在持久化文件中
        with open(data_file, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        assert "delivery_payloads" not in on_disk
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
