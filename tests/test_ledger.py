"""额度账本状态机单元测试。"""
from __future__ import annotations

import threading

import pytest

from app.services import (
    ConflictError,
    LedgerService,
    QuotaExceededError,
    delivery_payload_key,
    fingerprint,
    receipt_id_for,
)
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


def test_two_publications_same_payload_are_independent_deliveries(ledger):
    """两个稳定标识提交完全相同的摘要与额度：各自独立的记录/回执/计数。"""
    first = ledger.submit("PUB-1", "完全相同的匿名摘要", 3)
    second = ledger.submit("PUB-2", "完全相同的匿名摘要", 3)

    # 两份发布均完成，回执互不相同（回执只由各自发布标识派生）
    assert first["stage"] == "completed"
    assert second["stage"] == "completed"
    assert first["receipt_id"] == receipt_id_for("PUB-1")
    assert second["receipt_id"] == receipt_id_for("PUB-2")
    assert first["receipt_id"] != second["receipt_id"]

    state = ledger.state()
    # 额度按两次提交分别扣减：3 + 3 = 6
    assert state["used"] == 6
    assert state["available"] == 4
    assert [p["publication_id"] for p in state["publications"]] == ["PUB-1", "PUB-2"]
    # 接收端保留两份各自的接收记录，交付次数各为 1
    assert [(d["publication_id"], d["receipt_id"], d["delivered_count"]) for d in state["deliveries"]] == [
        ("PUB-1", receipt_id_for("PUB-1"), 1),
        ("PUB-2", receipt_id_for("PUB-2"), 1),
    ]

    # 同标识重传仍返回各自原回执、不扣额、交付次数不增加
    again_1 = ledger.submit("PUB-1", "完全相同的匿名摘要", 3)
    again_2 = ledger.submit("PUB-2", "完全相同的匿名摘要", 3)
    assert again_1["receipt_id"] == first["receipt_id"]
    assert again_2["receipt_id"] == second["receipt_id"]
    state = ledger.state()
    assert state["available"] == 4
    assert all(d["delivered_count"] == 1 for d in state["deliveries"])
    assert len(state["deliveries"]) == 2


def test_legacy_shared_delivery_ledger_converges_after_reopen(tmp_path):
    """旧版本受影响账本重开：补建缺失的本标识接收记录并安全收敛。"""
    import json

    data_file = tmp_path / "legacy.json"
    shared_delivery = {
        "publication_id": "PUB-A",
        "summary": "相同摘要",
        "summary_fingerprint": fingerprint("相同摘要"),
        "amount": 3,
        "receipt_id": receipt_id_for("PUB-A"),
        "delivered_count": 1,
        "accepted_at": 1000.0,
    }
    # 旧版本磁盘现场：
    # - PUB-A 正常完成；PUB-B 相同载荷“接收后断电”永久 frozen；
    # - 接收端只有 PUB-A 的记录，跨标识载荷索引指向 PUB-A；
    # - PUB-C 在接收端落盘前断电（载荷索引无记录），应保持 frozen。
    on_disk = {
        "version": 1,
        "publications": {
            "PUB-A": {
                "publication_id": "PUB-A", "summary": "相同摘要",
                "summary_fingerprint": fingerprint("相同摘要"), "amount": 3,
                "stage": "completed", "receipt_id": receipt_id_for("PUB-A"),
                "frozen_at": 1000.0, "completed_at": 1001.0,
            },
            "PUB-B": {
                "publication_id": "PUB-B", "summary": "相同摘要",
                "summary_fingerprint": fingerprint("相同摘要"), "amount": 3,
                "stage": "frozen", "receipt_id": receipt_id_for("PUB-B"),
                "frozen_at": 1002.0, "completed_at": None,
            },
            "PUB-C": {
                "publication_id": "PUB-C", "summary": "另一摘要",
                "summary_fingerprint": fingerprint("另一摘要"), "amount": 2,
                "stage": "frozen", "receipt_id": receipt_id_for("PUB-C"),
                "frozen_at": 1003.0, "completed_at": None,
            },
        },
        "deliveries": {"PUB-A": shared_delivery},
        "delivery_payloads": {delivery_payload_key("相同摘要", 3): dict(shared_delivery)},
    }
    data_file.write_text(json.dumps(on_disk), encoding="utf-8")

    ledger = LedgerService(JsonStore(str(data_file)), total_quota=10)

    with ledger.store.lock:
        # 迁移只补建有旧版本共享痕迹的 PUB-B，不臆造 PUB-C 的接收证据
        assert set(ledger.store.data["deliveries"]) == {"PUB-A", "PUB-B"}
        assert "delivery_payloads" not in ledger.store.data
        repaired = ledger.store.data["deliveries"]["PUB-B"]
    assert repaired["receipt_id"] == receipt_id_for("PUB-B")
    assert repaired["delivered_count"] == 1
    assert repaired["repaired_from_legacy"] is True

    recovered = ledger.recover_pending()
    assert [p["publication_id"] for p in recovered] == ["PUB-B"]
    state = ledger.state()
    stages = {p["publication_id"]: p["stage"] for p in state["publications"]}
    assert stages == {"PUB-A": "completed", "PUB-B": "completed", "PUB-C": "frozen"}
    receipts = {d["publication_id"]: d["receipt_id"] for d in state["deliveries"]}
    assert receipts == {"PUB-A": receipt_id_for("PUB-A"), "PUB-B": receipt_id_for("PUB-B")}
    # 无错误完成（PUB-C 仍冻结）也无永久冻结（PUB-B 已收敛）；额度 = 3+3+2
    assert state["frozen_amount"] == 2
    assert state["available"] == 2

    # PUB-C 随后以相同载荷重传：补接收并自愈收敛，不二次扣额
    healed = ledger.submit("PUB-C", "另一摘要", 2)
    assert healed["stage"] == "completed"
    state = ledger.state()
    assert state["frozen_amount"] == 0
    assert state["available"] == 2
    assert [d["publication_id"] for d in state["deliveries"]] == ["PUB-A", "PUB-B", "PUB-C"]
    assert all(d["delivered_count"] == 1 for d in state["deliveries"])
