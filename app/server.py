"""Flask 服务装配：交付页面、健康响应与交付/恢复 API。

环境变量：

- ``TOTAL_QUOTA``：总额度（默认 10）；
- ``DATA_FILE``  ：账本 JSON 路径（默认 ./data/ledger.json）；
- ``AUTO_RECOVER``：启动时是否自动收敛 frozen 发布（默认 1）。
"""
from __future__ import annotations

import os

from flask import Flask, jsonify, request, Response

from .page import render_page
from .services import ConflictError, LedgerService, QuotaExceededError
from .store import JsonStore


def create_app(
    total_quota: int | None = None,
    data_file: str | None = None,
    auto_recover: bool | None = None,
) -> Flask:
    total_quota = int(total_quota if total_quota is not None else os.environ.get("TOTAL_QUOTA", "10"))
    data_file = data_file or os.environ.get("DATA_FILE", "data/ledger.json")
    if auto_recover is None:
        auto_recover = os.environ.get("AUTO_RECOVER", "1") != "0"
    allow_reset = os.environ.get("ALLOW_RESET", "0") == "1"

    store = JsonStore(data_file)
    ledger = LedgerService(store, total_quota)

    if auto_recover:
        ledger.recover_pending()

    app = Flask(__name__)
    app.ledger = ledger  # 暴露给测试

    def error(message: str, status: int) -> tuple[Response, int]:
        return jsonify({"error": message}), status

    @app.get("/health")
    def health() -> Response:
        s = ledger.state()
        return jsonify(
            {
                "status": "ok",
                "total_quota": s["total_quota"],
                "available": s["available"],
                "frozen_amount": s["frozen_amount"],
                "publications": len(s["publications"]),
                "deliveries": len(s["deliveries"]),
            }
        )

    @app.get("/")
    def index() -> tuple[str, int]:
        return render_page(ledger.state()), 200

    @app.get("/api/state")
    def api_state() -> Response:
        return jsonify(ledger.state())

    @app.post("/api/deliveries")
    def api_deliver() -> tuple[Response, int] | Response:
        payload = request.get_json(silent=True) or {}
        pub_id = str(payload.get("publication_id", "")).strip()
        summary = payload.get("summary")
        amount = payload.get("amount")
        crash = bool(payload.get("crash_after_receive", False))
        if not pub_id or not isinstance(summary, str) or not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return error("publication_id(非空)、summary(字符串)、amount(正整数) 均为必填", 400)
        try:
            publication = ledger.submit(pub_id, summary, amount, crash_after_receive=crash)
        except ConflictError as exc:
            return error(str(exc), 409)
        except QuotaExceededError as exc:
            return error(str(exc), 402)
        # crash=True 时不会走到这里：进程已在接收端落盘后硬退出。
        return jsonify({**publication, "state": ledger.state()})

    @app.post("/api/recover")
    def api_recover() -> Response:
        recovered = ledger.recover_pending()
        return jsonify({"recovered": recovered, "state": ledger.state()})

    @app.post("/internal/reset")
    def api_reset() -> tuple[Response, int] | Response:
        # 仅供 Compose verify 冒烟使用（默认关闭）。
        if not allow_reset:
            return error("reset 未启用", 404)
        with store.lock:
            store.data["publications"] = {}
            store.data["deliveries"] = {}
            store.data.pop("delivery_payloads", None)
            store.flush()
        return jsonify({"ok": True, "state": ledger.state()})

    return app


def main() -> None:
    app = create_app()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    # 单进程 + 多线程：所有写操作在全局持久化锁与按标识锁内串行化。
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
