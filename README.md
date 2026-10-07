# 匿名统计交付 · 总额度账本

研究机构向协作方发布匿名统计时的数据官演示工程：以**稳定发布标识**提交
摘要与消耗，建立总额度账本；页面持续展示可用/冻结额度、发布阶段与接收方
回执。每个稳定发布标识都是一次**可独立追溯的交付**：即使两个标识提交的
匿名摘要与消耗额度完全相同，也各自拥有独立的接收方记录、回执与交付计数，
额度按两次提交分别扣减。同时保证在超额拒绝、相同载荷重传、载荷冲突、以及
“接收后断电”恢复等场景下，同一标识最终只有一条发布记录、一份回执、一次
额度冻结，恢复绝不复用另一标识的接收证据。

## 模型与状态机

```
正常两阶段预留：
  ① 本端写 frozen（额度冻结，占用总额度）→ 落盘
  ② 接收端按发布标识持久化该标识自己的摘要 + 回执（交付次数 = 1）→ 落盘
  ③ 本端 frozen → completed（发布终结）→ 落盘

接收后断电（crash_after_receive）：
  ①②落盘后、③之前 os._exit(99) 硬退出
  磁盘现场 = 本端 frozen（额度仍保留）+ 接收端该标识的冻结回执（次数 1）
  重开 → 按发布标识查询接收端本标识的冻结回执 → 收敛为原发布 completed
```

**核心不变量**

- **发布标识相互独立**：两个标识即使摘要指纹与额度完全相同，接收端也分别
  持久化两条记录、两份不同回执，交付次数各为 1，额度按两次提交扣减；
  重开或恢复只按本标识查本标识的接收证据，不把另一标识的回执当作本次结果。
- 已完成消耗 + 未终结冻结保留额 ≤ 总额度；页面与 `/api/state` 实时可见。
- 同一发布标识：
  - 相同载荷重传/并发提交 → 返回原回执，不再扣额、交付次数仍为 1（按标识锁 + 全局持久化锁）；
  - 改动摘要或额度 → HTTP 409 冲突，不新增回执、不动账；
  - 新标识超额交付 → HTTP 402 拒绝，接收端不新增回执。
- 接收端为每个标识持久化**首份**摘要与回执，永不被覆盖；同一标识的并发
  相同提交、重复投递、恢复重试最终只形成一条记录、一份回执、一次冻结。
- **遗留账本自愈**：早期版本曾以“摘要指纹+额度”做跨标识去重，导致第二
  个标识共享首份接收记录、断电后永久 frozen。再次打开旧账本时会凭磁盘上
  的载荷索引为缺失的标识补建其本标识接收记录（标记 `repaired_from_legacy`）
  并安全收敛；断电早于接收端落盘（无任何接收痕迹）的发布仍保持 frozen，
  由相同载荷重传自愈，不会被错误完成。

## 文件

| 文件 | 作用 |
| --- | --- |
| `app/store.py` | JSON 整库原子落盘（临时文件 + fsync + `os.replace`），断电只留完整旧版/新版 |
| `app/services.py` | 额度账本与接收端状态机：`submit` / `recover_pending` / `receiver_accept` |
| `app/page.py` | 交付页面渲染（每 2 秒轮询，展示可用/冻结/阶段/回执） |
| `app/server.py` | Flask 应用：`/`、`/health`、`/api/state`、`POST /api/deliveries`、`POST /api/recover` |
| `scripts/build_page.py` | 构建静态页面制品并检查必备要素 |
| `scripts/verify_smoke.py` | Compose `verify` 服务脚本 |
| `tests/` | 状态机/API/真实杀进程断电恢复测试（含双标识相同载荷与遗留账本收敛，17 项） |
| `Dockerfile`、`docker-compose.yml` | 容器化与编排 |

## 本地运行（无需 Docker）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
TOTAL_QUOTA=10 HOST=127.0.0.1 PORT=8080 .venv/bin/python -m app.server
# 页面 http://127.0.0.1:8080/  健康检查 /health
```

## Compose 运行

宿主端口可通过 `HOST_PORT` 配置（默认 8080）：

```bash
HOST_PORT=9090 docker compose up -d delivery   # 交付页面与健康响应
curl http://127.0.0.1:9090/health
```

数据持久化于命名卷 `ledger-data`（容器内 `/data/ledger.json`），断电重开
自动收敛（`AUTO_RECOVER=1`）。

## verify 编排

```bash
docker compose up --build verify
# 或并行输出： docker compose up --build
```

`verify` 等待 `delivery` 健康检查通过后启动，**完成即退出**并以退出码报告
结果（0 = 全部通过）。它：

1. **先构建交付页面**（`scripts/build_page.py`）并检查额度总览/可用额度/
   冻结额度/发布阶段/接收方回执五个必备要素；
2. 再以 **API/HTTP 冒烟**复核：消耗 6 → 可用 4 与一份回执；再消耗 5 →
   402 拒绝且接收端无新回执；同载荷重传 → 原回执不扣额；改摘要/改额度 →
   409；两个标识以相同摘要与额度提交 → 各自独立的接收记录与回执、次数各
   1、额度按两次扣减；
3. **真实子进程断电演练**：第一份相同载荷正常交付，第二份以另一标识提交
   相同载荷并 `os._exit` 断电 → 重开核对磁盘上两个标识各有接收记录 →
   只收敛第二份，两份发布均完成、两份回执、交付次数各 1、额度只扣两次；
4. **同时运行代码测试**（pytest，与冒烟并行）。

本机等价运行（无 Docker 时）：

```bash
.venv/bin/python -m pytest -q tests
DELIVERY_URL=http://127.0.0.1:8080 .venv/bin/python scripts/verify_smoke.py
```

## HTTP 接口

| 方法 路径 | 说明 |
| --- | --- |
| `GET /health` | 健康响应：状态、总额度、可用、冻结、记录数 |
| `GET /` | 交付页面（持续轮询展示） |
| `GET /api/state` | 账本只读视图 |
| `POST /api/deliveries` | `{publication_id, summary, amount, crash_after_receive?}` |
| `POST /api/recover` | 按发布标识查询冻结回执并收敛所有 frozen 发布 |

`POST /api/deliveries` 的状态码：`200` 受理/幂等重传；`409` 同标识载荷
冲突；`402` 总额度不足；`400` 参数非法。开启 `crash_after_receive` 时
服务端在接收端落盘后硬退出（HTTP 连接中断），用于演练“接收后断电”。
