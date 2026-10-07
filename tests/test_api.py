"""HTTP API 冒烟（Flask test client）。"""
from __future__ import annotations

from app.server import create_app


def make_app(tmp_path):
    return create_app(total_quota=10, data_file=str(tmp_path / "ledger.json"), auto_recover=False)


def test_health_and_page(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["status"] == "ok"
    assert health.get_json()["available"] == 10

    page = client.get("/")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    for marker in ("额度总览", "可用额度", "冻结额度", "发布阶段", "接收方回执"):
        assert marker in body


def test_delivery_rejection_conflict_and_idempotency(tmp_path):
    client = make_app(tmp_path).test_client()

    res = client.post("/api/deliveries", json={"publication_id": "PUB-1", "summary": "摘要", "amount": 6})
    assert res.status_code == 200
    assert res.get_json()["stage"] == "completed"
    assert res.get_json()["state"]["available"] == 4

    # 已有完成发布后，新标识消耗 5 必须拒绝
    rejected = client.post("/api/deliveries", json={"publication_id": "PUB-2", "summary": "另一摘要", "amount": 5})
    assert rejected.status_code == 402
    state = client.get("/api/state").get_json()
    assert [d["publication_id"] for d in state["deliveries"]] == ["PUB-1"]

    # 同标识改动额度 → 冲突
    conflict = client.post("/api/deliveries", json={"publication_id": "PUB-1", "summary": "摘要", "amount": 5})
    assert conflict.status_code == 409
    changed = client.post("/api/deliveries", json={"publication_id": "PUB-1", "summary": "改过", "amount": 6})
    assert changed.status_code == 409

    # 相同载荷重传 → 原回执、不再扣额
    retry = client.post("/api/deliveries", json={"publication_id": "PUB-1", "summary": "摘要", "amount": 6})
    assert retry.status_code == 200
    body = retry.get_json()
    assert body["receipt_id"] == res.get_json()["receipt_id"]
    assert body["state"]["available"] == 4


def test_bad_payload_returns_400(tmp_path):
    client = make_app(tmp_path).test_client()
    assert client.post("/api/deliveries", json={"publication_id": "", "summary": "x", "amount": 1}).status_code == 400
    assert client.post("/api/deliveries", json={"publication_id": "x", "summary": 1, "amount": 1}).status_code == 400
    assert client.post("/api/deliveries", json={"publication_id": "x", "summary": "y", "amount": 0}).status_code == 400
