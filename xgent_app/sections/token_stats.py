# Token 用量统计与价格报表。
# 依赖前面 sections 注入的：UserDataManager, GlobalRecorder, BotMemoryDB, MessageType,
# InlineKeyboardButton, InlineKeyboardMarkup, constants, html, logger 等。
import json
import io
import time
from datetime import datetime
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
    """从 DB 读取 token_usage 行，解析 metadata，返回标量记录列表。
    不传 start/end 表示读全量（交互式报表在前端按时间段过滤）。"""
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


def _aggregate(records: List[Dict[str, Any]], auto_merge: bool):
    """后端聚合（保留供 caption 用，报表内交互式聚合在前端 JS 完成）。"""
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

        day = datetime.fromtimestamp(r['ts']).strftime('%Y-%m-%d')
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


# --- ☆ HTML 渲染（交互式单页）☆ ---

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


# 模板用占位符 + replace 填值，避免 f-string 转义 JS/CSS 大括号。
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Token 统计报表</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; background: #f5f6f8; color: #222; }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 20px 16px 60px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .meta { color: #888; font-size: 13px; margin-bottom: 14px; }
  .controls { background: #fff; border-radius: 10px; padding: 14px 16px; box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom: 18px; }
  .controls h2 { font-size: 14px; margin: 0 0 10px; color: #555; }
  .row { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; margin-bottom: 8px; font-size: 13px; }
  .row label { color: #666; margin-right: 2px; }
  input[type=number], input[type=datetime-local], select, textarea {
    padding: 4px 8px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; background: #fff; font-family: inherit;
  }
  .btn { padding: 4px 10px; border: 0; border-radius: 6px; background: #4a90e2; color: #fff; cursor: pointer; font-size: 13px; }
  .btn.ghost { background: #eee; color: #333; }
  .btn.green { background: #2ecc71; }
  details { margin-top: 8px; border: 1px solid #eee; border-radius: 8px; padding: 8px 10px; }
  summary { cursor: pointer; font-size: 13px; color: #555; font-weight: 600; }
  table.pt { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 6px; }
  table.pt th, table.pt td { padding: 4px; border-bottom: 1px solid #f0f0f0; text-align: left; }
  table.pt input { width: 70px; padding: 2px 4px; border: 1px solid #ddd; border-radius: 4px; font-size: 12px; }
  table.pt input.mname { width: 150px; }
  .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 18px; }
  .card { background: #fff; border-radius: 10px; padding: 12px 14px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  .card .k { font-size: 12px; color: #888; }
  .card .v { font-size: 19px; font-weight: 600; margin-top: 3px; }
  .chart-box { background: #fff; border-radius: 10px; padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom: 16px; }
  .chart-box h2 { font-size: 14px; margin: 0 0 10px; }
  canvas { max-height: 340px; }
  table.tbl { width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  table.tbl th, table.tbl td { padding: 8px 10px; text-align: right; border-bottom: 1px solid #f0f0f0; font-size: 13px; }
  table.tbl th { background: #fafbfc; font-weight: 600; }
  table.tbl td.l, table.tbl th.l { text-align: left; }
  table.tbl tr:hover { background: #fafbfc; }
  .hint { font-size: 11px; color: #999; margin-top: 4px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>📊 Token 统计报表</h1>
  <div class="meta">生成时间：__GEN_TIME__ · 共 __COUNT__ 条原始记录 · 所有选项在本页内实时调整</div>

  <div class="controls">
    <h2>🎛️ 选项</h2>
    <div class="row">
      <label>时间段：</label>
      <input type="datetime-local" id="start" value="__START_ISO__">
      <span>～</span>
      <input type="datetime-local" id="end" value="__END_ISO__">
      <button class="btn ghost" id="presetAll">全部</button>
      <button class="btn ghost" id="preset7">最近7天</button>
      <button class="btn ghost" id="preset30">最近30天</button>
      <button class="btn ghost" id="presetToNow">截止到此刻</button>
    </div>
    <div class="row">
      <label>相同模型自动合并（最小交集）：</label>
      <input type="checkbox" id="autoMerge" __AUTO_MERGE__>
      <label>指标：</label>
      <select id="metric">
        <option value="token" __M_TOKEN__>Token 用量</option>
        <option value="cost" __M_COST__>费用 (USD)</option>
      </select>
    </div>

    <details><summary>🔗 手动合并表（规范名 = 成员，逗号分隔；优先于自动合并）</summary>
      <table class="pt" id="mergeTable"><thead><tr><th>规范名</th><th>成员（逗号分隔）</th><th></th></tr></thead><tbody></tbody></table>
      <button class="btn" id="addMerge">➕ 添加规则</button>
      <button class="btn green" id="applyMerge">应用合并表</button>
    </details>

    <details><summary>💵 模型价格表（每百万 token / USD；未配置按 default 或 0）</summary>
      <table class="pt" id="priceTable"><thead><tr><th>模型</th><th>输入</th><th>输出</th><th>缓存</th><th></th></tr></thead><tbody></tbody></table>
      <button class="btn" id="addPrice">➕ 添加价格</button>
      <button class="btn green" id="applyPrice">应用价格表</button>
    </details>

    <details><summary>📦 配置导入/导出（把改过的价格表/合并表搬走或回填）</summary>
      <textarea id="cfgIO" style="width:100%;height:120px;font-size:11px;" placeholder='{"priceTable":{...},"mergeMap":{...}}'></textarea>
      <button class="btn" id="cfgOut">导出当前配置到文本框</button>
      <button class="btn" id="cfgIn">从文本框导入配置</button>
    </details>
  </div>

  <div class="summary" id="summary"></div>
  <div class="chart-box"><h2>📈 折线图 · <span id="lineMetric">Token</span> 趋势（按天）</h2><canvas id="line"></canvas></div>
  <div class="chart-box"><h2>🥧 饼图 · 各模型费用占比</h2><canvas id="pie"></canvas></div>
  <div class="chart-box"><h2>📋 明细表格</h2>
    <table class="tbl" id="tbl"><thead><tr>
      <th class="l">模型</th><th>调用</th><th>输入</th><th>输出</th><th>缓存</th><th>思考</th><th>总计</th><th>费用</th><th>单价(in/out/cache)</th>
    </tr></thead><tbody></tbody></table>
  </div>
  <div class="hint">价格表/合并表的改动会自动存到浏览器 localStorage（同文件同路径记忆）。图表依赖 Chart.js CDN，离线时仅表格可用。</div>
</div>
<script>
const RECORDS = __RECORDS_JSON__;
const DEFAULT_PRICE = __PRICE_JSON__;
const DEFAULT_MERGE = __MERGE_JSON__;

const LS_KEY = 'xgent_token_stats_cfg_v1';
function loadCfg() { try { const s = localStorage.getItem(LS_KEY); if (s) return JSON.parse(s); } catch(e){} return null; }
function saveCfg(cfg) { try { localStorage.setItem(LS_KEY, JSON.stringify(cfg)); } catch(e){} }

// --- 合并算法（与后端 _build_clusters 一致）---
function lcsSubstr(a, b) {
  if (!a || !b) return '';
  let best = '', bestLen = 0;
  for (let i = 0; i < a.length; i++) for (let j = 0; j < b.length; j++) {
    let k = 0; while (i + k < a.length && j + k < b.length && a[i+k] === b[j+k]) k++;
    if (k > bestLen) { bestLen = k; best = a.substr(i, k); }
  }
  return best;
}
function commonSubstr(names) {
  if (!names.length) return '';
  let base = names[0];
  for (let i = 1; i < names.length; i++) { base = lcsSubstr(base, names[i]); if (!base) break; }
  return base;
}
function buildClusters(names, threshold) {
  threshold = threshold || 0.6;
  names = Array.from(new Set(names)).sort();
  const clusters = [];
  for (const n of names) {
    let placed = false;
    for (const cl of clusters) {
      const arr = cl.concat([n]);
      const rep = commonSubstr(arr);
      const short = Math.min.apply(null, arr.map(x => x.length));
      if (rep && short > 0 && rep.length / short >= threshold) { cl.push(n); placed = true; break; }
    }
    if (!placed) clusters.push([n]);
  }
  const out = {};
  for (const cl of clusters) {
    let rep = commonSubstr(cl) || cl[0];
    if (rep.length < 2) rep = cl.reduce((a, b) => a.length <= b.length ? a : b);
    out[rep] = cl;
  }
  return out;
}
function resolveName(name, mergeMap, clusters, autoMerge) {
  for (const canon in mergeMap) if (mergeMap[canon].indexOf(name) >= 0) return canon;
  if (autoMerge) for (const canon in clusters) if (clusters[canon].indexOf(name) >= 0) return canon;
  return name;
}
function modelPrice(model, pt) { return pt[model] || pt['default'] || {input:0,output:0,cached:0}; }
function computeCost(r, p) { return r.input/1e6*p.input + r.output/1e6*p.output + r.cached/1e6*p.cached; }

const STATE = {
  startMs: __START_MS__,
  endMs: __END_MS__,
  autoMerge: __AUTO_MERGE_JS__,
  metric: __METRIC_JS__,
  priceTable: Object.assign({}, DEFAULT_PRICE),
  mergeMap: Object.assign({}, DEFAULT_MERGE),
};
const saved = loadCfg();
if (saved) {
  if (saved.priceTable) STATE.priceTable = saved.priceTable;
  if (saved.mergeMap) STATE.mergeMap = saved.mergeMap;
}

function aggregate() {
  const r0 = RECORDS.filter(r => (!STATE.startMs || r.ts * 1000 >= STATE.startMs) && (!STATE.endMs || r.ts * 1000 <= STATE.endMs));
  const rawNames = Array.from(new Set(r0.map(r => r.model)));
  const clusters = STATE.autoMerge ? buildClusters(rawNames) : {};
  const perModel = {}, perDay = {};
  for (const r of r0) {
    const m = resolveName(r.model, STATE.mergeMap, clusters, STATE.autoMerge);
    const p = modelPrice(m, STATE.priceTable);
    const cost = computeCost(r, p);
    if (!perModel[m]) perModel[m] = {count:0,input:0,output:0,cached:0,reasoning:0,total:0,cost:0};
    const pm = perModel[m]; pm.count++; pm.input+=r.input; pm.output+=r.output; pm.cached+=r.cached; pm.reasoning+=r.reasoning; pm.total+=r.total; pm.cost+=cost;
    const d = new Date(r.ts * 1000).toISOString().slice(0, 10);
    if (!perDay[d]) perDay[d] = {};
    if (!perDay[d][m]) perDay[d][m] = {input:0,output:0,cached:0,total:0,cost:0,count:0};
    const pd = perDay[d][m]; pd.input+=r.input; pd.output+=r.output; pd.cached+=r.cached; pd.total+=r.total; pd.cost+=cost; pd.count++;
  }
  const grand = {count:r0.length, input:0, output:0, cached:0, reasoning:0, total:0, cost:0};
  for (const m in perModel) { const pm = perModel[m]; grand.input+=pm.input; grand.output+=pm.output; grand.cached+=pm.cached; grand.reasoning+=pm.reasoning; grand.total+=pm.total; grand.cost+=pm.cost; }
  return {records: r0, perModel: perModel, perDay: perDay, grand: grand, clusters: clusters};
}

function fmt(n) { if (n >= 1e6) return (n/1e6).toFixed(2)+'M'; if (n >= 1e3) return (n/1e3).toFixed(2)+'K'; return n; }
function fmtCost(c) { if (!c) return '—'; if (c < 0.01) return '$'+c.toFixed(6); return '$'+c.toFixed(4); }

let lineChart = null, pieChart = null;
const COLORS = ['#4dc9f6','#f67019','#f53794','#acc236','#166a8f','#00a950','#58595b','#8549ba','#6c0498','#0555b8','#e6c229','#cd040b','#5d3a1a','#0b5394'];
function color(i) { return COLORS[i % COLORS.length]; }

function render() {
  const agg = aggregate();
  const s = document.getElementById('summary');
  s.innerHTML = '';
  const cards = [
    ['调用次数', agg.grand.count],
    ['输入 token', fmt(agg.grand.input)],
    ['输出 token', fmt(agg.grand.output)],
    ['缓存命中', fmt(agg.grand.cached)],
    ['总 token', fmt(agg.grand.total)],
    ['总费用', fmtCost(agg.grand.cost)],
  ];
  cards.forEach(function(c) { const d = document.createElement('div'); d.className = 'card'; d.innerHTML = '<div class="k">'+c[0]+'</div><div class="v">'+c[1]+'</div>'; s.appendChild(d); });
  document.getElementById('lineMetric').textContent = STATE.metric === 'cost' ? '费用(USD)' : 'Token';

  const days = Object.keys(agg.perDay).sort();
  const models = Object.keys(agg.perModel).sort(function(a, b) { return agg.perModel[b].total - agg.perModel[a].total; });

  if (lineChart) lineChart.destroy();
  const lineData = models.map(function(m, i) {
    return { label: m, data: days.map(function(d) {
      const v = agg.perDay[d][m];
      if (!v) return 0;
      return STATE.metric === 'cost' ? Number((v.cost||0).toFixed(6)) : (v.total||0);
    }), borderWidth: 2, tension: 0.3, pointRadius: 2, borderColor: color(i), backgroundColor: color(i) };
  });
  lineChart = new Chart(document.getElementById('line'), {
    type: 'line',
    data: { labels: days, datasets: lineData },
    options: { responsive: true, plugins: { legend: { position: 'bottom' } }, scales: { y: { beginAtZero: true } } }
  });

  if (pieChart) pieChart.destroy();
  const pieLabels = models.filter(function(m) { return agg.perModel[m].cost > 0; });
  const pieData = pieLabels.map(function(m) { return Number((agg.perModel[m].cost||0).toFixed(6)); });
  pieChart = new Chart(document.getElementById('pie'), {
    type: 'doughnut',
    data: { labels: pieLabels.length ? pieLabels : models, datasets: [{ data: pieLabels.length ? pieData : models.map(function(m){return 1;}), backgroundColor: models.map(function(_, i){return color(i);}) }] },
    options: { responsive: true, plugins: { legend: { position: 'right' } }, cutout: '45%' }
  });

  const tb = document.querySelector('#tbl tbody'); tb.innerHTML = '';
  models.forEach(function(m) {
    const pm = agg.perModel[m]; const p = modelPrice(m, STATE.priceTable);
    const tr = document.createElement('tr');
    tr.innerHTML = '<td class="l">'+m+'</td><td>'+pm.count+'</td><td>'+fmt(pm.input)+'</td><td>'+fmt(pm.output)+'</td><td>'+fmt(pm.cached)+'</td><td>'+fmt(pm.reasoning)+'</td><td>'+fmt(pm.total)+'</td><td>'+fmtCost(pm.cost)+'</td><td>$'+p.input+'/M·$'+p.output+'/M·$'+p.cached+'/M</td>';
    tb.appendChild(tr);
  });
}

// --- 控件 ---
function toLocalInput(ms) {
  if (!ms) return '';
  const d = new Date(ms);
  const p = function(n) { return String(n).padStart(2, '0'); };
  return d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function fromLocalInput(v) { if (!v) return null; const t = new Date(v).getTime(); return isNaN(t) ? null : t; }

function bindChange() {
  STATE.startMs = fromLocalInput(document.getElementById('start').value);
  STATE.endMs = fromLocalInput(document.getElementById('end').value);
  STATE.autoMerge = document.getElementById('autoMerge').checked;
  STATE.metric = document.getElementById('metric').value;
  render();
}
['start', 'end', 'autoMerge', 'metric'].forEach(function(id) {
  document.getElementById(id).addEventListener('change', bindChange);
});

function setRangeDays(n) {
  const now = Date.now();
  STATE.startMs = n ? now - n * 86400000 : null;
  STATE.endMs = now;
  document.getElementById('start').value = n ? toLocalInput(STATE.startMs) : '';
  document.getElementById('end').value = toLocalInput(now);
  render();
}
document.getElementById('presetAll').onclick = function() {
  STATE.startMs = null; STATE.endMs = null;
  document.getElementById('start').value = ''; document.getElementById('end').value = '';
  render();
};
document.getElementById('preset7').onclick = function() { setRangeDays(7); };
document.getElementById('preset30').onclick = function() { setRangeDays(30); };
document.getElementById('presetToNow').onclick = function() {
  STATE.endMs = Date.now();
  document.getElementById('end').value = toLocalInput(STATE.endMs);
  render();
};

// 手动合并表
function renderMergeTable() {
  const tb = document.querySelector('#mergeTable tbody'); tb.innerHTML = '';
  Object.keys(STATE.mergeMap).forEach(function(canon) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td><input class="mname" value="'+canon+'"></td><td><input style="width:100%" value="'+STATE.mergeMap[canon].join(', ')+'"></td><td><button class="btn ghost" data-del="1">✕</button></td>';
    tr.querySelector('[data-del]').onclick = function() { delete STATE.mergeMap[canon]; renderMergeTable(); };
    tb.appendChild(tr);
  });
}
document.getElementById('addMerge').onclick = function() {
  const n = '新规范' + Date.now();
  STATE.mergeMap[n] = [];
  renderMergeTable();
};
document.getElementById('applyMerge').onclick = function() {
  const newMap = {};
  document.querySelectorAll('#mergeTable tbody tr').forEach(function(tr) {
    const inputs = tr.querySelectorAll('input');
    const c = inputs[0].value.trim();
    const m = inputs[1].value.split(',').map(function(s){return s.trim();}).filter(Boolean);
    if (c && m.length) newMap[c] = m;
  });
  STATE.mergeMap = newMap;
  saveCfg({priceTable: STATE.priceTable, mergeMap: STATE.mergeMap});
  renderMergeTable(); render();
};

// 价格表
function renderPriceTable() {
  const tb = document.querySelector('#priceTable tbody'); tb.innerHTML = '';
  Object.keys(STATE.priceTable).forEach(function(m) {
    const p = STATE.priceTable[m];
    const tr = document.createElement('tr');
    tr.innerHTML = '<td><input class="mname" value="'+m+'"></td><td><input type="number" step="0.01" value="'+p.input+'"></td><td><input type="number" step="0.01" value="'+p.output+'"></td><td><input type="number" step="0.01" value="'+p.cached+'"></td><td><button class="btn ghost" data-del="1">✕</button></td>';
    tr.querySelector('[data-del]').onclick = function() { delete STATE.priceTable[m]; renderPriceTable(); };
    tb.appendChild(tr);
  });
}
document.getElementById('addPrice').onclick = function() {
  STATE.priceTable['新模型' + Date.now()] = {input:0, output:0, cached:0};
  renderPriceTable();
};
document.getElementById('applyPrice').onclick = function() {
  const newPt = {};
  document.querySelectorAll('#priceTable tbody tr').forEach(function(tr) {
    const inputs = tr.querySelectorAll('input');
    const m = inputs[0].value.trim();
    if (!m) return;
    newPt[m] = {input: Number(inputs[1].value) || 0, output: Number(inputs[2].value) || 0, cached: Number(inputs[3].value) || 0};
  });
  STATE.priceTable = newPt;
  saveCfg({priceTable: STATE.priceTable, mergeMap: STATE.mergeMap});
  renderPriceTable(); render();
};

// 配置导入/导出
document.getElementById('cfgOut').onclick = function() {
  document.getElementById('cfgIO').value = JSON.stringify({priceTable: STATE.priceTable, mergeMap: STATE.mergeMap}, null, 2);
};
document.getElementById('cfgIn').onclick = function() {
  try {
    const c = JSON.parse(document.getElementById('cfgIO').value);
    if (c.priceTable) STATE.priceTable = c.priceTable;
    if (c.mergeMap) STATE.mergeMap = c.mergeMap;
    saveCfg(c);
    renderMergeTable(); renderPriceTable(); render();
  } catch (e) { alert('配置 JSON 格式错误: ' + e.message); }
};

renderMergeTable(); renderPriceTable(); render();
</script>
</body>
</html>
"""


def build_token_stats_html(
    records: List[Dict[str, Any]],
    price_table: Dict[str, Dict[str, float]],
    merge_map: Dict[str, List[str]],
    options: Dict[str, Any],
) -> str:
    """生成交互式 HTML 报表：时间段/合并/指标/价格表/合并表全部在网页内实时调整。
    内嵌全量 records，前端按当前控件值过滤+聚合+重绘。"""
    start = options.get('start')
    end = options.get('end')
    auto_merge = bool(options.get('auto_merge', True))
    metric = options.get('metric', 'token') or 'token'
    gen_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    records_json = json.dumps(records, ensure_ascii=False)
    price_json = json.dumps(price_table, ensure_ascii=False)
    merge_json = json.dumps(merge_map, ensure_ascii=False)

    start_ms = 'null' if start is None else int(start * 1000)
    end_ms = 'null' if end is None else int(end * 1000)
    start_iso = datetime.fromtimestamp(start).strftime('%Y-%m-%dT%H:%M') if start else ''
    end_iso = datetime.fromtimestamp(end).strftime('%Y-%m-%dT%H:%M') if end else ''

    auto_merge_chk = 'checked' if auto_merge else ''
    auto_merge_js = 'true' if auto_merge else 'false'
    m_token_sel = 'selected' if metric == 'token' else ''
    m_cost_sel = 'selected' if metric == 'cost' else ''
    metric_js = json.dumps(metric)

    html_doc = (
        _HTML_TEMPLATE
        .replace('__GEN_TIME__', gen_time)
        .replace('__COUNT__', str(len(records)))
        .replace('__START_ISO__', start_iso)
        .replace('__END_ISO__', end_iso)
        .replace('__AUTO_MERGE__', auto_merge_chk)
        .replace('__M_TOKEN__', m_token_sel)
        .replace('__M_COST__', m_cost_sel)
        .replace('__RECORDS_JSON__', records_json)
        .replace('__PRICE_JSON__', price_json)
        .replace('__MERGE_JSON__', merge_json)
        .replace('__START_MS__', start_ms)
        .replace('__END_MS__', end_ms)
        .replace('__AUTO_MERGE_JS__', auto_merge_js)
        .replace('__METRIC_JS__', metric_js)
    )
    return html_doc


# --- ☆ 导出命令 ☆ ---

def _ts_from_text(text: str) -> Optional[float]:
    """支持 YYYY-MM-DD / YYYY-MM-DD HH:MM。失败返回 None。"""
    text = (text or '').strip()
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d', '%Y-%m-%dT%H:%M'):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


async def export_token_stats_html(
    start: Optional[float] = None,
    end: Optional[float] = None,
    auto_merge: bool = True,
    metric: str = 'token',
) -> Tuple[Optional[bytes], Dict[str, Any]]:
    """导出交互式 HTML 报表。读全量 records，内嵌价格表/合并表；
    start/end/auto_merge/metric 仅作为网页打开时的默认值，用户可在页面内随时改。"""
    records = await _load_token_records()
    if not records:
        return None, {'count': 0, 'first': None, 'last': None}
    price_table = _get_price_table()
    merge_map = _get_merge_map()
    html_doc = build_token_stats_html(
        records, price_table, merge_map,
        {'start': start, 'end': end, 'auto_merge': auto_merge, 'metric': metric},
    )
    return html_doc.encode('utf-8'), {
        'count': len(records),
        'first': records[0]['ts'],
        'last': records[-1]['ts'],
    }


async def cmd_token_stats(update, context):
    """/stats [days] — 导出 token 统计 HTML 报表（交互式，所有选项在网页内调整）。
    例：/stats        导出全部，网页打开后可在页面内选时间段
        /stats 7      默认显示最近7天（页面内仍可改）
        /stats 2025-01-01 2025-02-01  默认显示该区间"""
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
            period = f"_{datetime.fromtimestamp(start).strftime('%m%d')}-{datetime.fromtimestamp(end).strftime('%m%d')}"
        fname = f"token_stats{period}.html"
        first = datetime.fromtimestamp(info['first']).strftime('%Y-%m-%d')
        last = datetime.fromtimestamp(info['last']).strftime('%Y-%m-%d')
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=io.BytesIO(data),
            filename=fname,
            caption=(f"📊 Token 统计报表（交互式）\n"
                     f"共 {info['count']} 条记录 · 时间跨度 {first} ~ {last}\n"
                     f"打开网页后可在页面内调整时间段/合并/指标/价格表"),
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
                "未配置价格的模型按 'default' 或 0 计费。\n"
                "💡 也可在导出的统计报表 HTML 内直接调整价格表。")
    lines = ["💵 <b>模型价格表</b>（每百万 token / USD）", ""]
    for m in sorted(price_table.keys()):
        p = price_table[m]
        lines.append(f"• <b>{m}</b>: in ${p['input']} · out ${p['output']} · cache ${p['cached']}")
    lines.append("")
    lines.append("💡 'default' 作为未配置模型的兜底单价。也可在报表 HTML 内调整。")
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
