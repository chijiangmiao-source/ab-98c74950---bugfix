"""额度账本状态机单元测试。"""
from __future__ import annotations

import threading

import pytest

from app.services import ConflictError, LedgerService, QuotaExceededError, receipt_id_for
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
