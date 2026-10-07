"""交付页面渲染（无外部前端构建链，构建脚本将其落为静态制品）。"""
from __future__ import annotations

import json
from typing import Any

REQUIRED_MARKERS = ("额度总览", "可用额度", "冻结额度", "发布阶段", "接收方回执")


def render_page(initial_state: dict[str, Any] | None = None) -> str:
    # 内嵌 <script> 的 JSON 需转义 <，防止载荷中的 </script> 提前闭合脚本。
    initial = json.dumps(initial_state or {}, ensure_ascii=False).replace("<", "\\u003c")
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>匿名统计交付 · 总额度账本</title>
<style>
:root { color-scheme: light; }
body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; margin: 0; background: #f5f6f8; color: #1f2933; }
header { background: #1f4e79; color: #fff; padding: 16px 24px; }
header h1 { margin: 0; font-size: 18px; }
header .meta { font-size: 12px; opacity: .85; margin-top: 4px; }
main { max-width: 1080px; margin: 20px auto; padding: 0 16px; display: grid; gap: 16px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.card { background: #fff; border-radius: 10px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.card .label { font-size: 12px; color: #6b7280; }
.card .value { font-size: 28px; font-weight: 700; margin-top: 4px; }
.card.available .value { color: #15803d; }
.card.frozen .value { color: #b45309; }
.card.completed .value { color: #1f4e79; }
.card.used .value { color: #374151; }
section.panel { background: #fff; border-radius: 10px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
section.panel h2 { margin: 0 0 12px; font-size: 15px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef0f2; vertical-align: top; }
th { color: #6b7280; font-weight: 600; background: #fafbfc; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; }
.badge.completed { background: #dcfce7; color: #166534; }
.badge.frozen { background: #fef3c7; color: #92400e; }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
form.delivery { display: grid; grid-template-columns: 160px 1fr 90px auto; gap: 8px; align-items: center; }
form.delivery input { padding: 8px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 13px; }
button { padding: 8px 14px; border: 0; border-radius: 6px; background: #1f4e79; color: #fff; font-size: 13px; cursor: pointer; }
button.secondary { background: #6b7280; }
#result { margin-top: 10px; font-size: 13px; white-space: pre-wrap; }
.ok { color: #15803d; } .err { color: #b91c1c; }
.muted { color: #9ca3af; font-size: 12px; }
label.check { font-size: 12px; color: #6b7280; display: flex; gap: 4px; align-items: center; }
@media (max-width: 720px) { form.delivery { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>匿名统计交付 · 总额度账本</h1>
  <div class="meta">研究机构 → 协作方 ｜ 稳定发布标识 ｜ 每个标识各自持久化首份摘要与接收方回执</div>
</header>
<main>
  <div class="cards" id="cards"><!-- 额度总览 --></div>

  <section class="panel">
    <h2>提交交付（消耗额度）</h2>
    <form class="delivery" id="delivery-form">
      <input name="publication_id" placeholder="发布标识，如 PUB-2026-001" required>
      <input name="summary" placeholder="匿名统计摘要（稳定载荷）" required>
      <input name="amount" type="number" min="1" placeholder="消耗" required>
      <button type="submit">提交交付</button>
      <label class="check"><input type="checkbox" name="crash_after_receive"> 接收后断电（演练）</label>
      <span></span><span></span>
      <button type="button" class="secondary" id="recover-btn">触发断电恢复</button>
    </form>
    <div id="result" class="muted">就绪。页面每 2 秒自动刷新账本与回执。</div>
  </section>

  <section class="panel">
    <h2>本端发布记录（<!-- 发布阶段 -->冻结 → 完成）</h2>
    <table>
      <thead><tr><th>发布标识</th><th>摘要</th><th>消耗</th><th>发布阶段</th><th>回执</th><th>冻结/完成时间</th></tr></thead>
      <tbody id="publications"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
    </table>
  </section>

  <section class="panel">
    <h2>接收方回执（接收端按发布标识持久化，每个标识交付次数恒为 1）</h2>
    <table>
      <thead><tr><th>发布标识</th><th>首份摘要指纹</th><th>金额</th><th>接收方回执</th><th>交付次数</th></tr></thead>
      <tbody id="receipts"><tr><td colspan="5" class="muted">加载中…</td></tr></tbody>
    </table>
  </section>
</main>
<script id="initial-state" type="application/json">__INITIAL__</script>
<script>
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "—";

function cards(s) {
  const items = [
    ["available", "可用额度", s.available],
    ["frozen", "冻结额度（未终结保留额）", s.frozen_amount],
    ["completed", "已完成消耗", s.completed_amount],
    ["used", "已占用合计", s.used],
    ["", "总额度", s.total_quota],
  ];
  return items.map(([cls, label, value]) =>
    `<div class="card ${cls}"><div class="label">${label}</div><div class="value">${esc(value)}</div></div>`).join("");
}

function render(s) {
  document.getElementById("cards").innerHTML = cards(s);
  const pubs = (s.publications || []);
  document.getElementById("publications").innerHTML = pubs.length ? pubs.map((p) => `
    <tr>
      <td class="mono">${esc(p.publication_id)}</td>
      <td>${esc(p.summary)}<div class="muted mono">${esc(p.summary_fingerprint.slice(0,16))}…</div></td>
      <td>${esc(p.amount)}</td>
      <td><span class="badge ${p.stage}">${p.stage === "completed" ? "已完成" : "冻结中"}</span></td>
      <td class="mono">${esc(p.receipt_id)}</td>
      <td class="muted">${fmtTime(p.frozen_at)}<br>${fmtTime(p.completed_at)}</td>
    </tr>`).join("")
    : `<tr><td colspan="6" class="muted">暂无发布</td></tr>`;
  const dels = s.deliveries || [];
  document.getElementById("receipts").innerHTML = dels.length ? dels.map((d) => `
    <tr>
      <td class="mono">${esc(d.publication_id)}</td>
      <td class="mono">${esc(d.summary_fingerprint.slice(0,24))}…</td>
      <td>${esc(d.amount)}</td>
      <td class="mono">${esc(d.receipt_id)}</td>
      <td><strong>${esc(d.delivered_count)}</strong></td>
    </tr>`).join("")
    : `<tr><td colspan="5" class="muted">接收端暂无交付</td></tr>`;
}

async function refresh() {
  try { render(await (await fetch("/api/state")).json()); } catch (e) { /* 保留上次渲染 */ }
}

function show(msg, ok) {
  const el = document.getElementById("result");
  el.className = ok ? "ok" : "err";
  el.textContent = msg;
}

document.getElementById("delivery-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const body = {
    publication_id: f.publication_id.value.trim(),
    summary: f.summary.value,
    amount: Number(f.amount.value),
    crash_after_receive: f.crash_after_receive.checked,
  };
  try {
    const res = await fetch("/api/deliveries", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
    const data = await res.json();
    if (res.ok) {
      show(`已受理：阶段 ${data.stage} ｜ 回执 ${data.receipt_id} ｜ 当前可用额度 ${data.state.available}`, true);
    } else {
      show(`被拒绝（HTTP ${res.status}）：${data.error}`, false);
    }
  } catch (e) {
    show(body.crash_after_receive
      ? "连接中断：接收端已接受交付，本端在记账完成前退出（接收后断电）。重启后将自动收敛。"
      : ("请求失败：" + e), false);
  }
  refresh();
});

document.getElementById("recover-btn").addEventListener("click", async () => {
  const res = await fetch("/api/recover", {method: "POST"});
  const data = await res.json();
  show(`恢复收敛 ${data.recovered.length} 个发布`, true);
  refresh();
});

try { render(JSON.parse(document.getElementById("initial-state").textContent)); } catch (e) {}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
""".replace("__INITIAL__", initial)
