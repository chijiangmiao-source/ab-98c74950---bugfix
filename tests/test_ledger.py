"""额度账本状态机单元测试。"""
from __future__ import annotations

import json
import threading

import pytest

from app.services import ConflictError, LedgerService, QuotaExceededError, fingerprint, receipt_id_for
from app.store import JsonStore


@pytest.fixture()
def ledger(tmp_path):
    return LedgerService(JsonStore(str(tmp_path / "ledger.json")), total_quota=10)


def test_submit_consumes_quota_and_issues_one_receipt(ledger):
    pub = ledger.submit("PUB-1", "2026Q3 匿名统计摘要", 6)
    assert pub["stage"] == "completed"
    assert pub["receipt_id"] == receipt_id_for("PUB-1")

    state = ledger.state()
    assert state["available"] == 4
    assert state["used"] == 6
    assert state["completed_amount"] == 6
    assert state["frozen_amount"] == 0
    assert len(state["publications"]) == 1
    # 一份回执，交付次数为 1
    assert len(state["deliveries"]) == 1
    assert state["deliveries"][0]["delivered_count"] == 1


def test_consume_five_after_six_is_rejected_without_new_receipt(ledger):
    ledger.submit("PUB-1", "摘要甲", 6)
    # 新的发布标识再消耗 5：6+5>10，必须拒绝
    with pytest.raises(QuotaExceededError):
        ledger.submit("PUB-2", "摘要乙", 5)

    state = ledger.state()
    assert state["available"] == 4
    # 接收端不能新增回执
    assert [d["publication_id"] for d in state["deliveries"]] == ["PUB-1"]


def test_identical_retransmission_returns_same_receipt_and_does_not_charge(ledger):
    first = ledger.submit("PUB-1", "同一份摘要", 6)
    second = ledger.submit("PUB-1", "同一份摘要", 6)
    assert first["receipt_id"] == second["receipt_id"]

    state = ledger.state()
    assert state["available"] == 4
    assert len(state["publications"]) == 1
    assert state["deliveries"][0]["delivered_count"] == 1


def test_changed_summary_or_amount_conflicts(ledger):
    ledger.submit("PUB-1", "原始摘要", 6)
    with pytest.raises(ConflictError):
        ledger.submit("PUB-1", "被改动的摘要", 6)
    with pytest.raises(ConflictError):
        ledger.submit("PUB-1", "原始摘要", 5)

    state = ledger.state()
    # 冲突不改变额度，也不新增回执
    assert state["available"] == 4
    assert len(state["publications"]) == 1
    assert len(state["deliveries"]) == 1
    assert state["publications"][0]["summary"] == "原始摘要"


def test_quota_invariant_completed_plus_frozen_within_total(tmp_path):
    ledger = LedgerService(JsonStore(str(tmp_path / "l.json")), total_quota=10)
    ledger.submit("PUB-A", "甲", 4)
    # 手工制造一个 frozen（模拟断电窗口），冻结额同样占用总额度
    with ledger.store.lock:
        ledger.store.data["publications"]["PUB-B"] = {
            "publication_id": "PUB-B", "summary": "乙", "summary_fingerprint": "x",
            "amount": 5, "stage": "frozen", "receipt_id": "RC-x",
            "frozen_at": 0, "completed_at": None,
        }
        ledger.store.flush()
    assert ledger.state()["available"] == 1
    with pytest.raises(QuotaExceededError):
        ledger.submit("PUB-C", "丙", 2)


def test_concurrent_identical_submissions_collapse_to_one_record(ledger):
    results: list = []
    errors: list = []

    def worker() -> None:
        try:
            results.append(ledger.submit("PUB-CONC", "并发相同载荷", 3))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 12
    assert len({r["receipt_id"] for r in results}) == 1
    state = ledger.state()
    assert len(state["publications"]) == 1
    assert len(state["deliveries"]) == 1
    assert state["deliveries"][0]["delivered_count"] == 1
    assert state["available"] == 7


def test_receiver_keeps_first_summary_and_receipt(ledger):
    first = ledger.receiver_accept("PUB-R", "首份摘要", 2)
    again = ledger.receiver_accept("PUB-R", "首份摘要", 2)
    assert again["receipt_id"] == first["receipt_id"]
    assert again["delivered_count"] == 1
    with pytest.raises(ConflictError):
        ledger.receiver_accept("PUB-R", "不同摘要", 2)

    with ledger.store.lock:
        delivery = ledger.store.data["deliveries"]["PUB-R"]
    assert delivery["summary"] == "首份摘要"


def test_distinct_publication_ids_with_identical_payload_are_independent(ledger):
    """两个标识、相同摘要与额度：各自独立的发布记录、回执与一次交付计数。"""
    summary, amount = "完全相同的匿名摘要", 4
    first = ledger.submit("PUB-A", summary, amount)
    second = ledger.submit("PUB-B", summary, amount)

    assert first["stage"] == second["stage"] == "completed"
    assert first["receipt_id"] != second["receipt_id"]
    assert first["receipt_id"] == receipt_id_for("PUB-A")
    assert second["receipt_id"] == receipt_id_for("PUB-B")

    state = ledger.state()
    # 两份发布均完成；额度按两次提交扣减（4+4=8）
    assert {p["publication_id"] for p in state["publications"]} == {"PUB-A", "PUB-B"}
    assert all(p["stage"] == "completed" for p in state["publications"])
    assert state["available"] == 2
    assert state["used"] == 8
    assert state["completed_amount"] == 8
    # 接收端两份对应回执，交付次数各为 1
    by_id = {d["publication_id"]: d for d in state["deliveries"]}
    assert set(by_id) == {"PUB-A", "PUB-B"}
    for pub_id in ("PUB-A", "PUB-B"):
        assert by_id[pub_id]["summary"] == summary
        assert by_id[pub_id]["amount"] == amount
        assert by_id[pub_id]["delivered_count"] == 1
        assert by_id[pub_id]["receipt_id"] == receipt_id_for(pub_id)


def test_identical_payload_third_delivery_still_enforces_quota(ledger):
    """相同载荷的不同标识各扣一次额度，超额的新标识仍被拒绝。"""
    ledger.submit("PUB-A", "重复摘要", 4)
    ledger.submit("PUB-B", "重复摘要", 4)
    # 4+4+4>10：第三个同载荷新标识必须 402，接收端不新增回执
    with pytest.raises(QuotaExceededError):
        ledger.submit("PUB-C", "重复摘要", 4)

    state = ledger.state()
    assert state["available"] == 2
    assert {d["publication_id"] for d in state["deliveries"]} == {"PUB-A", "PUB-B"}
    assert all(d["delivered_count"] == 1 for d in state["deliveries"])


def test_legacy_ledger_with_aliased_receiver_record_converges_on_reopen(tmp_path):
    """旧版共享索引导致第二标识缺接收记录（completed）：重开安全补齐。"""
    data_file = str(tmp_path / "ledger.json")
    summary, amount = "旧账本摘要", 3
    digest = fingerprint(summary)
    first_delivery = {
        "publication_id": "PUB-OLD-1",
        "summary": summary,
        "summary_fingerprint": digest,
        "amount": amount,
        "receipt_id": receipt_id_for("PUB-OLD-1"),
        "delivered_count": 1,
        "accepted_at": 100.0,
    }
    legacy = {
        "version": 1,
        "publications": {
            "PUB-OLD-1": {
                "publication_id": "PUB-OLD-1", "summary": summary,
                "summary_fingerprint": digest, "amount": amount,
                "stage": "completed", "receipt_id": receipt_id_for("PUB-OLD-1"),
                "frozen_at": 100.0, "completed_at": 101.0,
            },
            # 旧版：第二标识拿到“完成”响应，但接收端只有首份记录。
            "PUB-OLD-2": {
                "publication_id": "PUB-OLD-2", "summary": summary,
                "summary_fingerprint": digest, "amount": amount,
                "stage": "completed", "receipt_id": receipt_id_for("PUB-OLD-2"),
                "frozen_at": 102.0, "completed_at": 103.0,
            },
        },
        "deliveries": {"PUB-OLD-1": first_delivery},
        "delivery_payloads": {f"{digest}:{amount}": dict(first_delivery)},
    }
    with open(data_file, "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)

    ledger = LedgerService(JsonStore(data_file), total_quota=10)
    state = ledger.state()
    assert {p["publication_id"] for p in state["publications"]} == {"PUB-OLD-1", "PUB-OLD-2"}
    assert all(p["stage"] == "completed" for p in state["publications"])
    by_id = {d["publication_id"]: d for d in state["deliveries"]}
    assert set(by_id) == {"PUB-OLD-1", "PUB-OLD-2"}
    assert by_id["PUB-OLD-2"]["receipt_id"] == receipt_id_for("PUB-OLD-2")
    assert by_id["PUB-OLD-2"]["delivered_count"] == 1
    assert by_id["PUB-OLD-2"].get("migrated_from_legacy_alias") is True
    assert state["used"] == 6
    with open(data_file, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert "delivery_payloads" not in on_disk


def test_legacy_ledger_frozen_alias_converges_on_reopen(tmp_path):
    """旧版第二标识接收后断电永久冻结：重开补齐证据并收敛完成。"""
    data_file = str(tmp_path / "ledger.json")
    summary, amount = "旧账本断电摘要", 3
    digest = fingerprint(summary)
    first_delivery = {
        "publication_id": "PUB-FZ-1",
        "summary": summary,
        "summary_fingerprint": digest,
        "amount": amount,
        "receipt_id": receipt_id_for("PUB-FZ-1"),
        "delivered_count": 1,
        "accepted_at": 100.0,
    }
    legacy = {
        "version": 1,
        "publications": {
            "PUB-FZ-1": {
                "publication_id": "PUB-FZ-1", "summary": summary,
                "summary_fingerprint": digest, "amount": amount,
                "stage": "completed", "receipt_id": receipt_id_for("PUB-FZ-1"),
                "frozen_at": 100.0, "completed_at": 101.0,
            },
            # 旧版：第二标识接收端落盘后断电，重开永远 frozen。
            "PUB-FZ-2": {
                "publication_id": "PUB-FZ-2", "summary": summary,
                "summary_fingerprint": digest, "amount": amount,
                "stage": "frozen", "receipt_id": receipt_id_for("PUB-FZ-2"),
                "frozen_at": 102.0, "completed_at": None,
            },
        },
        "deliveries": {"PUB-FZ-1": first_delivery},
        "delivery_payloads": {f"{digest}:{amount}": dict(first_delivery)},
    }
    with open(data_file, "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)

    ledger = LedgerService(JsonStore(data_file), total_quota=10)
    recovered = ledger.recover_pending()
    assert [p["publication_id"] for p in recovered] == ["PUB-FZ-2"]
    assert recovered[0]["stage"] == "completed"
    state = ledger.state()
    assert state["frozen_amount"] == 0
    assert state["available"] == 4
    by_id = {d["publication_id"]: d for d in state["deliveries"]}
    assert set(by_id) == {"PUB-FZ-1", "PUB-FZ-2"}
    assert by_id["PUB-FZ-2"]["receipt_id"] == receipt_id_for("PUB-FZ-2")
    assert all(d["delivered_count"] == 1 for d in state["deliveries"])
    # 再重开一次：稳定收敛，无遗留冻结
    again = LedgerService(JsonStore(data_file), total_quota=10)
    assert again.recover_pending() == []
    assert again.state()["frozen_amount"] == 0


def test_legacy_ledger_frozen_without_receiver_evidence_stays_frozen(tmp_path):
    """无接收证据的 frozen（断电早于接收落盘）不应被迁移误判为已接收。"""
    data_file = str(tmp_path / "ledger.json")
    summary, amount = "仅冻结摘要", 3
    digest = fingerprint(summary)
    legacy = {
        "version": 1,
        "publications": {
            "PUB-NODEL": {
                "publication_id": "PUB-NODEL", "summary": summary,
                "summary_fingerprint": digest, "amount": amount,
                "stage": "frozen", "receipt_id": receipt_id_for("PUB-NODEL"),
                "frozen_at": 100.0, "completed_at": None,
            },
        },
        "deliveries": {},
        "delivery_payloads": {},
    }
    with open(data_file, "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)

    ledger = LedgerService(JsonStore(data_file), total_quota=10)
    assert ledger.recover_pending() == []
    state = ledger.state()
    assert state["frozen_amount"] == 3
    assert state["deliveries"] == []
    # 同载荷重传自愈：补齐接收证据并收敛
    pub = ledger.submit("PUB-NODEL", summary, amount)
    assert pub["stage"] == "completed"
    assert ledger.state()["deliveries"][0]["delivered_count"] == 1

