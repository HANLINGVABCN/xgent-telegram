# Token 用量统计与价格报表。
# 依赖前面 sections 注入的：UserDataManager, GlobalRecorder, BotMemoryDB, MessageType,
# InlineKeyboardButton, InlineKeyboardMarkup, constants, html, logger 等。
import json
import io
import time
import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    from difflib import SequenceMatcher
except Exception:  # pragma: no cover
    SequenceMatcher = None


# --- ☆ 模型名合并（最小交集 = 最长公共子串聚类）☆ ---

def _lcs_substr(a: str, b: str) -> str:
    """两个字符串的最长公共子串。"""
    if not a or not b:
        return ""
    if SequenceMatcher is not None:
        m = SequenceMatcher(None, a, b)
        match = m.find_longest_match(0, len(a), 0, len(b))
        return a[match.a: match.a + match.size]
    # 纯手写兜底
    la, lb = len(a), len(b)
    best, best_len = "", 0
    for i in range(la):
        for j in range(lb):
            k = 0
            while i + k < la and j + k < lb and a[i + k] == b[j + k]:
                k += 1
            if k > best_len:
                best_len = k
                best = a[i:i + k]
    return best


def _common_substr(names: List[str]) -> str:
    """一组字符串的最长公共子串。"""
    if not names:
        return ""
    base = names[0]
    for s in names[1:]:
        base = _lcs_substr(base, s)
        if not base:
            break
    return base


def _build_clusters(names: List[str], threshold: float = 0.6) -> Dict[str, List[str]]:
    """贪心聚类：两两公共子串占较短串比例 >= threshold 归一簇。
    返回 {规范名(公共子串): [实际名...]}。"""
    names = sorted(set(names))
    clusters: List[List[str]] = []
    for n in names:
        placed = False
        for cl in clusters:
            rep = _common_substr(cl + [n])
            short = min(len(x) for x in cl + [n])
            if rep and short > 0 and len(rep) / short >= threshold:
                cl.append(n)
                placed = True
                break
        if not placed:
            clusters.append([n])
    result: Dict[str, List[str]] = {}
    for cl in clusters:
        rep = _common_substr(cl) or cl[0]
        # 避免规范名空或太短：取簇里最短的成员
        if len(rep) < 2:
            rep = min(cl, key=len)
        result[rep] = cl
    return result


def _resolve_name(name: str, merge_map: Dict[str, List[str]],
                  clusters: Dict[str, List[str]], auto_merge: bool) -> str:
    """手动绑定优先，自动聚类其次，原名兜底。"""
    for canon, members in merge_map.items():
        if name in members:
            return canon
    if auto_merge:
        for canon, members in clusters.items():
            if name in members:
                return canon
    return name


# --- ☆ 数据读取 ☆ ---

async def _load_token_records(start_ts: Optional[float] = None,
                              end_ts: Optional[float] = None) -> List[Dict[str, Any]]:
    """从 DB 读取 token_usage 行，解析 metadata，返回标量记录列表。"""
    db = await BotMemoryDB.get_instance()
    rows = await db.get_global_messages(limit=200000, include_types=[MessageType.TOKEN_USAGE])
    records: List[Dict[str, Any]] = []
    for r in rows:
        ts = r.get('timestamp') or 0
        if start_ts is not None and ts < start_ts:
            continue
        if end_ts is not None and ts > end_ts:
            continue
        meta_raw = r.get('metadata')
        meta = {}
        if meta_raw:
            try:
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except Exception:
                meta = {}
        model = meta.get('model') or ''
        usage = meta.get('usage') or {}
        if not model and not usage:
            continue  # 升级前旧记录，无结构化数据，跳过
        records.append({
            'ts': ts,
            'model': model or '未知',
            'input': int(usage.get('input_tokens') or 0),
            'output': int(usage.get('output_tokens') or 0),
            'cached': int(usage.get('cached_tokens') or 0),
            'reasoning': int(usage.get('reasoning_tokens') or 0),
            'total': int(usage.get('total_tokens') or 0),
        })
    records.sort(key=lambda x: x['ts'])
    return records


# --- ☆ 价格表 ☆ ---

def _get_price_table() -> Dict[str, Dict[str, float]]:
    raw = UserDataManager.get('model_price_table', {}) or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    out = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            out[k] = {
                'input': float(v.get('input', 0) or 0),
                'output': float(v.get('output', 0) or 0),
                'cached': float(v.get('cached', 0) or 0),
            }
    return out


def _get_merge_map() -> Dict[str, List[str]]:
    raw = UserDataManager.get('model_merge_map', {}) or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    out = {}
    for k, v in raw.items():
        if isinstance(v, list):
            out[k] = [str(x) for x in v if x]
    return out


def _model_price(model: str, price_table: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    if model in price_table:
        return price_table[model]
    return price_table.get('default') or {'input': 0.0, 'output': 0.0, 'cached': 0.0}


def _compute_cost(rec: Dict[str, Any], price: Dict[str, float]) -> float:
    # 单位：每百万 token 美元
    return (
        rec['input'] / 1_000_000 * price['input']
        + rec['output'] / 1_000_000 * price['output']
        + rec['cached'] / 1_000_000 * price['cached']
    )


# --- ☆ 聚合 ☆ ---

def _aggregate(records: List[Dict[str, Any]], auto_merge: bool):
    """返回 (per_model, per_day, grand_total)。"""
    merge_map = _get_merge_map()
    raw_names = list({r['model'] for r in records})
    clusters = _build_clusters(raw_names) if auto_merge else {}
    price_table = _get_price_table()

    per_model: Dict[str, Dict[str, Any]] = {}
    per_day: Dict[str, Dict[str, Any]] = {}

    for r in records:
        m = _resolve_name(r['model'], merge_map, clusters, auto_merge)
        price = _model_price(m, price_table)
        cost = _compute_cost(r, price)

        pm = per_model.setdefault(m, {
            'count': 0, 'input': 0, 'output': 0, 'cached': 0,
            'reasoning': 0, 'total': 0, 'cost': 0.0,
        })
        pm['count'] += 1
        pm['input'] += r['input']
        pm['output'] += r['output']
        pm['cached'] += r['cached']
        pm['reasoning'] += r['reasoning']
        pm['total'] += r['total']
        pm['cost'] += cost

        day = datetime.datetime.fromtimestamp(r['ts']).strftime('%Y-%m-%d')
        pd = per_day.setdefault(day, {})
        pm_d = pd.setdefault(m, {
            'input': 0, 'output': 0, 'cached': 0, 'total': 0, 'cost': 0.0, 'count': 0
        })
        pm_d['input'] += r['input']
        pm_d['output'] += r['output']
        pm_d['cached'] += r['cached']
        pm_d['total'] += r['total']
        pm_d['cost'] += cost
        pm_d['count'] += 1

    grand = {
        'count': len(records),
        'input': sum(r['input'] for r in records),
        'output': sum(r['output'] for r in records),
        'cached': sum(r['cached'] for r in records),
        'reasoning': sum(r['reasoning'] for r in records),
        'total': sum(r['total'] for r in records),
        'cost': sum(pm['cost'] for pm in per_model.values()),
    }
    return per_model, per_day, grand, (clusters, merge_map, price_table)


# --- ☆ HTML 渲染 ☆ ---

def _fmt_num(n: float) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.2f}K"
    return str(int(n))


def _fmt_cost(c: float) -> str:
    if c == 0:
        return "—"
    if c < 0.01:
        return f"${c:.6f}"
    return f"${c:.4f}"


def build_token_stats_html(
    records: List[Dict[str, Any]],
    per_model: Dict[str, Dict[str, Any]],
    per_day: Dict[str, Dict[str, Any]],
    grand: Dict[str, Any],
    options: Dict[str, Any],
) -> str:
    """生成自包含 HTML 报表（Chart.js 走 CDN，表格纯 HTML）。"""
    start = options.get('start')
    end = options.get('end')
    auto_merge = options.get('auto_merge', True)
    metric = options.get('metric', 'token')  # 'token' | 'cost'
    ctx = options.get('ctx_info', {})

    period_str = "全部"
    if start and end:
        period_str = f"{datetime.datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M')} ~ {datetime.datetime.fromtimestamp(end).strftime('%Y-%m-%d %H:%M')}"
    elif end:
        period_str = f"截至 {datetime.datetime.fromtimestamp(end).strftime('%Y-%m-%d %H:%M')}"

    gen_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 折线图数据
    days = sorted(per_day.keys())
    models = sorted(per_model.keys(), key=lambda m: per_model[m]['total'], reverse=True)
    line_datasets = []
    for m in models:
        data = []
        for d in days:
            v = per_day.get(d, {}).get(m, {})
            if metric == 'cost':
                data.append(round(v.get('cost', 0), 6))
            else:
                data.append(v.get('total', 0))
        line_datasets.append({'label': m, 'data': data})

    # 饼图数据（按模型总费用）
    pie_labels = models
    pie_data = [round(per_model[m]['cost'], 6) for m in models]

    # 表格行
    table_rows = []
    for m in models:
        pm = per_model[m]
        price = _model_price(m, _get_price_table())
        table_rows.append({
            'model': m,
            'count': pm['count'],
            'input': pm['input'],
            'output': pm['output'],
            'cached': pm['cached'],
            'reasoning': pm['reasoning'],
            'total': pm['total'],
            'cost': pm['cost'],
            'price_in': price['input'],
            'price_out': price['output'],
        })

    line_json = json.dumps(line_datasets, ensure_ascii=False)
    days_json = json.dumps(days, ensure_ascii=False)
    pie_json = json.dumps({'labels': pie_labels, 'data': pie_data}, ensure_ascii=False)
    table_json = json.dumps(table_rows, ensure_ascii=False)

    metric_label = "费用 (USD)" if metric == 'cost' else "Token 总量"
    merge_label = "开启（最小交集合并）" if auto_merge else "关闭（每个模型独立）"

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Token 统计报表</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; background: #f5f6f8; color: #222; }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px 60px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .meta {{ color: #888; font-size: 13px; margin-bottom: 18px; }}
  .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 24px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 14px 16px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  .card .k {{ font-size: 12px; color: #888; }}
  .card .v {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}
  .chart-box {{ background: #fff; border-radius: 10px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom: 20px; }}
  .chart-box h2 {{ font-size: 15px; margin: 0 0 12px; }}
  canvas {{ max-height: 360px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  th, td {{ padding: 9px 10px; text-align: right; border-bottom: 1px solid #f0f0f0; font-size: 13px; }}
  th {{ background: #fafbfc; font-weight: 600; }}
  td.l, th.l {{ text-align: left; }}
  tr:hover {{ background: #fafbfc; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>📊 Token 统计报表</h1>
  <div class="meta">生成时间：{gen_time} · 时间范围：{period_str} · 合并：{merge_label} · 指标：{metric_label}</div>
  <div class="summary">
    <div class="card"><div class="k">调用次数</div><div class="v">{grand['count']}</div></div>
    <div class="card"><div class="k">输入 token</div><div class="v">{_fmt_num(grand['input'])}</div></div>
    <div class="card"><div class="k">输出 token</div><div class="v">{_fmt_num(grand['output'])}</div></div>
    <div class="card"><div class="k">缓存命中</div><div class="v">{_fmt_num(grand['cached'])}</div></div>
    <div class="card"><div class="k">总 token</div><div class="v">{_fmt_num(grand['total'])}</div></div>
    <div class="card"><div class="k">总费用</div><div class="v">{_fmt_cost(grand['cost'])}</div></div>
  </div>
  <div class="chart-box">
    <h2>📈 折线图 · {metric_label} 趋势（按天）</h2>
    <canvas id="line"></canvas>
  </div>
  <div class="chart-box">
    <h2>🥧 饼图 · 各模型费用占比</h2>
    <canvas id="pie"></canvas>
  </div>
  <div class="chart-box">
    <h2>📋 明细表格</h2>
    <table id="tbl">
      <thead><tr>
        <th class="l">模型</th><th>调用</th><th>输入</th><th>输出</th><th>缓存</th><th>思考</th><th>总计</th><th>费用</th><th>单价(in/out)</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</div>
<script>
const days = {days_json};
const lineDatasets = {line_json};
const pie = {pie_json};
const tableRows = {table_json};
function fmt(n){{ if(n>=1e6) return (n/1e6).toFixed(2)+'M'; if(n>=1e3) return (n/1e3).toFixed(2)+'K'; return n; }}
function fmtCost(c){{ if(!c) return '—'; if(c<0.01) return '$'+c.toFixed(6); return '$'+c.toFixed(4); }}
new Chart(document.getElementById('line'), {{
  type: 'line',
  data: {{ labels: days, datasets: lineDatasets.map(d=>({{label:d.label,data:d.data,borderWidth:2,tension:.3,pointRadius:2}})) }},
  options: {{ responsive: true, plugins: {{ legend: {{ position: 'bottom' }} }}, scales: {{ y: {{ beginAtZero: true }} }} }}
}});
new Chart(document.getElementById('pie'), {{
  type: 'doughnut',
  data: {{ labels: pie.labels, datasets:[{{ data: pie.data, backgroundColor: ['#4dc9f6','#f67019','#f53794','#acc236','#166a8f','#00a950','#58595b','#8549ba','#6c0498','#0555b8'] }}] }},
  options: {{ responsive: true, plugins: {{ legend: {{ position: 'right' }} }}, cutout: '45%' }}
}});
const tb = document.getElementById('tbody');
tableRows.forEach(r=>{{
  const tr=document.createElement('tr');
  tr.innerHTML = '<td class="l">'+r.model+'</td><td>'+r.count+'</td><td>'+fmt(r.input)+'</td><td>'+fmt(r.output)+'</td><td>'+fmt(r.cached)+'</td><td>'+fmt(r.reasoning)+'</td><td>'+fmt(r.total)+'</td><td>'+fmtCost(r.cost)+'</td><td>$'+r.price_in+'/M · $'+r.price_out+'/M</td>';
  tb.appendChild(tr);
}});
</script>
</body>
</html>
"""
    return html_doc


# --- ☆ 导出命令 ☆ ---

def _ts_from_text(text: str) -> Optional[float]:
    """支持 YYYY-MM-DD / YYYY-MM-DD HH:MM。失败返回 None。"""
    text = (text or '').strip()
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d', '%Y-%m-%dT%H:%M'):
        try:
            return datetime.datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


async def export_token_stats_html(
    start: Optional[float] = None,
    end: Optional[float] = None,
    auto_merge: bool = True,
    metric: str = 'token',
) -> Tuple[Optional[bytes], Dict[str, Any]]:
    """生成 HTML bytes，返回 (html_bytes, info)。无数据返回 (None, info)。"""
    records = await _load_token_records(start, end)
    if not records:
        return None, {'count': 0, 'first': None, 'last': None}
    per_model, per_day, grand, _ctx = _aggregate(records, auto_merge)
    html_doc = build_token_stats_html(
        records, per_model, per_day, grand,
        {'start': start, 'end': end, 'auto_merge': auto_merge, 'metric': metric},
    )
    return html_doc.encode('utf-8'), {
        'count': grand['count'],
        'first': records[0]['ts'],
        'last': records[-1]['ts'],
        'cost': grand['cost'],
        'total': grand['total'],
    }


async def cmd_token_stats(update, context):
    """/stats [days] — 导出最近 N 天（默认全部）token 统计 HTML。
    例：/stats 7  最近7天；/stats 2025-01-01 2025-02-01  指定区间。"""
    args = (context.args or []) if context else []
    start = end = None
    if len(args) == 1 and args[0].isdigit():
        days = int(args[0])
        end = time.time()
        start = end - days * 86400
    elif len(args) >= 2:
        start = _ts_from_text(args[0])
        end = _ts_from_text(args[1]) or time.time()
    auto_merge = UserDataManager.get('stats_auto_merge', True)
    metric = UserDataManager.get('stats_metric', 'token')

    status = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="📊 正在生成 Token 统计报表...",
    )
    try:
        data, info = await export_token_stats_html(start, end, auto_merge, metric)
        if not data:
            await status.edit_text("⚠️ 暂无可统计的 token 记录（升级后产生的新记录才会被统计）。")
            return
        period = ""
        if start and end:
            period = f"_{datetime.datetime.fromtimestamp(start).strftime('%m%d')}-{datetime.datetime.fromtimestamp(end).strftime('%m%d')}"
        fname = f"token_stats{period}.html"
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=io.BytesIO(data),
            filename=fname,
            caption=(f"📊 Token 统计报表\n记录 {info['count']} 条 · "
                     f"总 token {_fmt_num(info['total'])} · 总费用 {_fmt_cost(info['cost'])}"),
        )
        await status.delete()
        await GlobalRecorder.record_system_op(f"导出 Token 统计报表 ({info['count']} 条)")
    except Exception as e:
        logger.error(f"Token 统计导出失败: {e}")
        try:
            await status.edit_text(f"⚠️ 导出失败: {e}")
        except Exception:
            pass


# --- ☆ 价格表 / 合并表 设置菜单（Telegram）☆ ---

def get_price_table_menu() -> InlineKeyboardMarkup:
    price_table = _get_price_table()
    models = sorted(price_table.keys())
    buttons = []
    for m in models[:20]:
        p = price_table[m]
        buttons.append([InlineKeyboardButton(
            f"{m}: in ${p['input']}/out ${p['output']}/cache ${p['cached']}",
            callback_data=CallbackDataStore.store(f"edit_price_{m}"),
        )])
    buttons.append([InlineKeyboardButton("➕ 添加/修改模型价格", callback_data="add_price_model")])
    buttons.append([InlineKeyboardButton("🔗 手动合并模型", callback_data="menu_merge_map")])
    buttons.append([InlineKeyboardButton("🔙 返回", callback_data="menu_timeout_settings")])
    return InlineKeyboardMarkup(buttons)


def build_price_table_text() -> str:
    price_table = _get_price_table()
    if not price_table:
        return ("💵 <b>模型价格表</b>\n\n"
                "当前为空。价格单位：<b>每百万 token 美元</b>。\n"
                "点击「➕ 添加/修改模型价格」设置，格式：input,output,cached\n"
                "未配置价格的模型按 'default' 或 0 计费。")
    lines = ["💵 <b>模型价格表</b>（每百万 token / USD）", ""]
    for m in sorted(price_table.keys()):
        p = price_table[m]
        lines.append(f"• <b>{m}</b>: in ${p['input']} · out ${p['output']} · cache ${p['cached']}")
    lines.append("")
    lines.append("💡 提示：'default' 作为未配置模型的兜底单价。")
    return "\n".join(lines)


async def cmd_price_table_menu(update, context):
    await update.callback_query.message.edit_text(
        build_price_table_text(),
        reply_markup=get_price_table_menu(),
        parse_mode=constants.ParseMode.HTML,
    )


async def handle_price_table_callbacks(update, context):
    """在 callbacks.py 的 handle_button_click 里调用。"""
    query = update.callback_query
    data = CallbackDataStore.get(query.data or "")
    if data == "menu_price_table":
        await cmd_price_table_menu(update, context)
        return True
    if data == "add_price_model":
        UserDataManager.set('state', BotState.SET_PRICE_MODEL)
        await query.message.reply_text(
            "➕ 请输入模型名和价格，格式：\n"
            "<code>模型名 input,output,cached</code>\n"
            "例: <code>gemini-3.6,0.5,1.5,0.1</code>\n"
            "（每百万 token 美元）发送 cancel 取消。",
            parse_mode=constants.ParseMode.HTML,
        )
        return True
    if data == "menu_merge_map":
        mm = _get_merge_map()
        if not mm:
            text = ("🔗 <b>手动模型合并表</b>\n\n当前为空。"
                    "格式：<code>规范名=实际名1,实际名2</code>\n"
                    "例: <code>gemini-3.6=1er-gemini-3.6,vsrfefv-gemini-3.6</code>")
        else:
            lines = ["🔗 <b>手动模型合并表</b>", ""]
            for k, v in mm.items():
                lines.append(f"• <b>{k}</b> ← {', '.join(v)}")
            text = "\n".join(lines)
        UserDataManager.set('state', BotState.SET_MERGE_MAP)
        await query.message.reply_text(
            text + "\n\n请输入合并规则（格式 规范名=实际名1,实际名2），或发送 <code>clear</code> 清空。发送 cancel 取消。",
            parse_mode=constants.ParseMode.HTML,
        )
        return True
    if data.startswith("edit_price_"):
        model = data[len("edit_price_"):]
        UserDataManager.set('state', BotState.SET_PRICE_MODEL)
        UserDataManager.set('_pending_price_model', model)
        await query.message.reply_text(
            f"✏️ 修改 <b>{model}</b> 的价格，格式：\n"
            "<code>input,output,cached</code>\n"
            "例: <code>0.5,1.5,0.1</code> 发送 cancel 取消。",
            parse_mode=constants.ParseMode.HTML,
        )
        return True
    return False


async def handle_price_table_state(update, context, text: str) -> bool:
    """处理 SET_PRICE_MODEL / SET_MERGE_MAP 状态的文本输入。返回是否已处理。"""
    state = UserDataManager.get('state')
    low = text.strip().lower()
    if state == BotState.SET_PRICE_MODEL:
        model = UserDataManager.get('_pending_price_model')
        if model:
            # 仅输入价格三元组
            parts = [p.strip() for p in text.split(',')]
            if len(parts) != 3:
                await update.message.reply_text("⚠️ 格式：input,output,cached，例 0.5,1.5,0.1")
                return True
            name = model
        else:
            # 模型名,input,output,cached
            parts = [p.strip() for p in text.split(',')]
            if len(parts) != 4:
                await update.message.reply_text("⚠️ 格式：模型名,input,output,cached")
                return True
            name, parts = parts[0], parts[1:]
        try:
            price = {'input': float(parts[0]), 'output': float(parts[1]), 'cached': float(parts[2])}
        except ValueError:
            await update.message.reply_text("⚠️ 价格必须是数字。")
            return True
        price_table = _get_price_table()
        price_table[name] = price
        UserDataManager.set('model_price_table', price_table)
        await UserDataManager.save_config('model_price_table', price_table)
        UserDataManager.set('state', BotState.IDLE)
        UserDataManager.set('_pending_price_model', None)
        await GlobalRecorder.record_system_op(f"设置模型价格: {name}")
        await update.message.reply_text(
            f"✅ {name} 价格已保存。",
            reply_markup=get_price_table_menu(),
        )
        return True
    if state == BotState.SET_MERGE_MAP:
        if low == 'clear':
            UserDataManager.set('model_merge_map', {})
            await UserDataManager.save_config('model_merge_map', {})
            UserDataManager.set('state', BotState.IDLE)
            await update.message.reply_text("✅ 手动合并表已清空。")
            return True
        if '=' not in text:
            await update.message.reply_text("⚠️ 格式：规范名=实际名1,实际名2")
            return True
        canon, _, rest = text.partition('=')
        canon = canon.strip()
        members = [m.strip() for m in rest.split(',') if m.strip()]
        if not canon or not members:
            await update.message.reply_text("⚠️ 规范名和成员不能为空。")
            return True
        mm = _get_merge_map()
        mm[canon] = members
        UserDataManager.set('model_merge_map', mm)
        await UserDataManager.save_config('model_merge_map', mm)
        UserDataManager.set('state', BotState.IDLE)
        await GlobalRecorder.record_system_op(f"添加手动合并: {canon}")
        await update.message.reply_text(
            f"✅ 合并规则已保存：{canon} ← {', '.join(members)}",
            reply_markup=get_timeout_settings_menu(),
        )
        return True
    return False
