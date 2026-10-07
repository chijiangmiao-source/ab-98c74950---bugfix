"""额度账本与接收方回执的状态机。

正常提交（两阶段预留）：

1. 本端按稳定发布标识写入 ``frozen`` 记录并落盘——额度被冻结，计入
   “未终结保留额”，回执标识由发布标识确定性派生，两边共享；
2. 接收端接受交付，持久化**首份**摘要与回执（交付次数 1）；
3. 本端将 ``frozen`` 收敛为 ``completed`` 并落盘，发布终结。

“接收后断电”（``crash_after_receive=True``）：步骤 2 落盘后、步骤 3
之前以 ``os._exit`` 硬退出。磁盘现场为：接收端持有冻结回执且交付次数为
1，本端发布停留在 ``frozen``、额度仍被保留。重开时 :meth:`recover_pending`
按发布标识查询接收端冻结回执，将原发布收敛为 ``completed``，全程只有一条
发布记录、一份回执、一次额度冻结。

不变量：

- 已完成消耗 + 未终结冻结保留额之和不得超过总额度；
- 同一标识相同载荷重传/并发提交幂等返回原回执，不再扣额；
- 同一标识改动摘要或额度 → 冲突，不新增回执、不改变额度；
- 接收端首份摘要与回执一旦持久化永不被覆盖。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Any

from .store import JsonStore


class ConflictError(Exception):
    """同一发布标识但摘要或消耗额度不一致（HTTP 409）。"""


class QuotaExceededError(Exception):
    """已完成与未终结保留额之和将超过总额度（HTTP 402）。"""


def receipt_id_for(pub_id: str) -> str:
    digest = hashlib.sha256(f"receipt:{pub_id}".encode("utf-8")).hexdigest()[:16]
    return f"RC-{digest}"


def fingerprint(summary: str) -> str:
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


def delivery_payload_key(summary: str, amount: int) -> str:
    summary_digest = fingerprint(summary)
    normalized_amount = int(amount)
    return f"{summary_digest}:{normalized_amount}"


class LedgerService:
    def __init__(self, store: JsonStore, total_quota: int):
        self.store = store
        self.total_quota = int(total_quota)
        # 同一发布标识的并发提交在此串行化，配合持久化保证“只形成一条”。
        self._pub_locks: dict[str, threading.Lock] = {}
        self._pub_locks_guard = threading.Lock()

    # -- 内部工具 ---------------------------------------------------------

    def _lock_for(self, pub_id: str) -> threading.Lock:
        with self._pub_locks_guard:
            lock = self._pub_locks.get(pub_id)
            if lock is None:
                lock = threading.Lock()
                self._pub_locks[pub_id] = lock
            return lock

    @staticmethod
    def _used(publications: dict[str, dict[str, Any]]) -> int:
        """已完成消耗 + 未终结冻结保留额。"""
        return sum(int(p["amount"]) for p in publications.values())

    @staticmethod
    def _same_payload(record: dict[str, Any], summary: str, amount: int) -> bool:
        return (
            record["summary_fingerprint"] == fingerprint(summary)
            and int(record["amount"]) == int(amount)
        )

    # -- 接收端 -----------------------------------------------------------

    def receiver_accept(self, pub_id: str, summary: str, amount: int) -> dict[str, Any]:
        """接收端接受交付，返回交付记录（含唯一回执）。

        首份摘要与回执在此生成并持久化；相同载荷重复/并发投递直接返回原
        记录（交付次数不增加）；不同载荷冲突，绝不覆盖首份摘要。
        """
        with self.store.lock:
            delivery = self.store.data["deliveries"].get(pub_id)
            if delivery is not None:
                if not self._same_payload(delivery, summary, amount):
                    raise ConflictError("接收端已存在不同载荷的首份交付")
                return dict(delivery)
            payload_key = delivery_payload_key(summary, amount)
            shared_delivery = self.store.data["delivery_payloads"].get(payload_key)
            if shared_delivery is not None:
                if not self._same_payload(shared_delivery, summary, amount):
                    raise ConflictError("接收端已存在不同载荷的首份交付")
                return dict(shared_delivery)
            delivery = {
                "publication_id": pub_id,
                "summary": summary,
                "summary_fingerprint": fingerprint(summary),
                "amount": int(amount),
                "receipt_id": receipt_id_for(pub_id),
                "delivered_count": 1,
                "accepted_at": time.time(),
            }
            self.store.data["deliveries"][pub_id] = delivery
            self.store.data["delivery_payloads"][payload_key] = dict(delivery)
            self.store.flush()
            return dict(delivery)

    # -- 本端（数据官） ---------------------------------------------------

    def submit(self, pub_id: str, summary: str, amount: int, crash_after_receive: bool = False) -> dict[str, Any]:
        """以稳定发布标识提交一次消耗交付。"""
        amount = int(amount)
        with self._lock_for(pub_id):
            with self.store.lock:
                publications = self.store.data["publications"]
                existing = publications.get(pub_id)
                if existing is not None:
                    if not self._same_payload(existing, summary, amount):
                        raise ConflictError(
                            f"发布标识 {pub_id} 已存在，但摘要或消耗额度不一致"
                        )
                    # 相同载荷重传：幂等返回原记录/回执。若上一任在终结前
                    # 断电（frozen）且接收端回执已在，则顺手收敛。
                    if existing["stage"] == "frozen":
                        delivery = self.store.data["deliveries"].get(pub_id)
                        if delivery is not None:
                            return self._complete(pub_id)
                    return dict(existing)

                used_others = self._used({k: v for k, v in publications.items() if k != pub_id})
                if used_others + amount > self.total_quota:
                    raise QuotaExceededError(
                        f"总额度 {self.total_quota} 不足：已占用 {used_others}，本次消耗 {amount}"
                    )

                # 阶段一：本端冻结额度并落盘（一次额度冻结）。
                now = time.time()
                publications[pub_id] = {
                    "publication_id": pub_id,
                    "summary": summary,
                    "summary_fingerprint": fingerprint(summary),
                    "amount": amount,
                    "stage": "frozen",
                    "receipt_id": receipt_id_for(pub_id),
                    "frozen_at": now,
                    "completed_at": None,
                }
                self.store.flush()

            # 阶段二：接收端接受并持久化首份摘要/回执。
            self.receiver_accept(pub_id, summary, amount)

            if crash_after_receive:
                # 接收后断电：两份落盘均已完成（本端 frozen、接收端交付），
                # 硬杀进程，不执行阶段三。
                os._exit(99)

            # 阶段三：本端终结发布。
            return self._complete(pub_id)

    def _complete(self, pub_id: str) -> dict[str, Any]:
        with self.store.lock:
            publication = self.store.data["publications"][pub_id]
            if publication["stage"] != "completed":
                publication["stage"] = "completed"
                publication["completed_at"] = time.time()
                self.store.flush()
            return dict(publication)

    # -- 断电恢复 ---------------------------------------------------------

    def recover_pending(self) -> list[dict[str, Any]]:
        """重开时收敛所有未终结发布。

        按发布标识查询接收端的冻结回执：查得到则将本端原发布（frozen）
        收敛为 completed；查不到（断电早于接收端落盘的极端窗口）则保留
        frozen，额度继续占用，留待恢复重试或相同载荷重传自愈。恢复重试
        天然幂等，不会二次扣额或新增回执。
        """
        recovered: list[dict[str, Any]] = []
        with self.store.lock:
            frozen_ids = sorted(
                pub_id
                for pub_id, pub in self.store.data["publications"].items()
                if pub["stage"] == "frozen"
            )
        for pub_id in frozen_ids:
            with self._lock_for(pub_id):
                with self.store.lock:
                    publication = self.store.data["publications"].get(pub_id)
                    if publication is None or publication["stage"] != "frozen":
                        continue
                    delivery = self.store.data["deliveries"].get(pub_id)
                    if delivery is None:
                        continue
                    if not self._same_payload(publication, delivery["summary"], int(delivery["amount"])):
                        # 理论不可达：冻结与接收端载荷同源。保留冻结待人工核查。
                        continue
                recovered.append(self._complete(pub_id))
        return recovered

    # -- 只读视图 ---------------------------------------------------------

    def state(self) -> dict[str, Any]:
        data = self.store.snapshot()
        publications = data["publications"]
        completed = sum(int(p["amount"]) for p in publications.values() if p["stage"] == "completed")
        frozen = sum(int(p["amount"]) for p in publications.values() if p["stage"] == "frozen")
        return {
            "total_quota": self.total_quota,
            "used": completed + frozen,
            "available": self.total_quota - completed - frozen,
            "completed_amount": completed,
            "frozen_amount": frozen,
            "publications": [publications[k] for k in sorted(publications)],
            "deliveries": [data["deliveries"][k] for k in sorted(data["deliveries"])],
        }
