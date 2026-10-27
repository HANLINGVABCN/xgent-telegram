// Navigation must remain usable if optional workbench/components scripts fail.
// Keep this small read-only surface independent of their module graph.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
const button = (text, onclick) => el('button', {type: 'button', class: 'wb-btn', onclick}, text);
const message = (text, kind = 'error') => el('div', {class: 'wb-' + kind, role: kind === 'error' ? 'alert' : 'status'}, text);
const query = values => '?' + new URLSearchParams(Object.entries(values).filter(([, value]) => value !== null && value !== undefined && value !== '')).toString();
const stamp = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12: false}) : '未记录时间';
async function wb(path, options) {
  const response = await fetch('/api/workbench/' + path, {credentials: 'same-origin', ...options});
  if (response.status === 401) window.dispatchEvent(new Event('xgent-auth-expired'));
  let data;
  try { data = await response.json(); } catch { throw new Error('搜索服务返回异常，请重试。'); }
  if (!response.ok || data.ok === false) throw new Error(data.error || `搜索失败 (${response.status})`);
  return data;
}

// Both the sidebar and dialog use exactly the same result renderer/query logic.
const roles = {title: '对话名称', user: '用户消息', assistant: 'AI 回复', tool: '工具结果', system: '系统消息'};
function highlighted(text, match) {
  const span = el('span');
  if (!match) { span.textContent = text; return span; }
  const parts = String(text).split(match);
  parts.forEach((part, index) => { if (index) span.append(el('mark', {}, match)); span.append(document.createTextNode(part)); });
  return span;
}
async function openResult(item) {
  // The workbench owns archived readers and exact history anchors. Reading a
  // result must not restore archives or switch Telegram's selected conversation.
  const event = new CustomEvent('xgent-search-open', {detail: item, cancelable: true});
  if (window.dispatchEvent(event)) {
    if (item.archived) throw new Error('只读历史页面尚未加载，请刷新网页后重试。');
    if (!await window.XGentConversations?.select(item.conversation_id)) throw new Error('未能打开目标对话，请重试。');
    location.hash = '#/chat';
    await window.XGentChat?.locate(item.message_key);
  }
}

export class ConversationSearch {
  constructor(container, {onOpen = () => {}, compact = false} = {}) {
    this.container = container;
    this.onOpen = onOpen;
    this.compact = compact;
    this.sequence = 0;
    this.options = null;
    this.items = [];
    this.cursor = null;
    container.addEventListener('keydown', event => {
      if (!this.options || !['ArrowDown','ArrowUp','Home','End'].includes(event.key) || !event.target.matches('button')) return;
      const buttons = [...container.querySelectorAll('button:not(:disabled)')];
      const index = buttons.indexOf(document.activeElement);
      event.preventDefault(); event.stopPropagation();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : Math.max(0, Math.min(buttons.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)));
      buttons[next]?.focus();
    });
  }
  reset() {
    this.sequence++;
    clearTimeout(this.timer);
    this.controller?.abort();
    this.options = null;
    this.container.removeAttribute('aria-busy');
  }
  search(q, filters = {}, immediate = false) {
    const options = {q: q.trim(), archive: filters.archive || 'all', kind: filters.kind || 'all'};
    if (JSON.stringify(options) === JSON.stringify(this.options)) return;
    this.reset(); this.options = options; this.items = []; this.cursor = null;
    if (!options.q) {
      this.container.replaceChildren(message('输入关键词，搜索全部对话名称和历史消息（含归档）。', 'note'));
      return;
    }
    this.container.replaceChildren(message('搜索中…', 'loading'));
    this.container.setAttribute('aria-busy', 'true');
    this.timer = setTimeout(() => this.load(), immediate ? 0 : 250);
  }
  async load(cursor = null) {
    const sequence = this.sequence, options = this.options;
    if (!options?.q) return;
    this.controller?.abort(); const controller = this.controller = new AbortController();
    const timeout = setTimeout(() => controller.abort('timeout'), 25000);
    this.loading = true; this.container.setAttribute('aria-busy', 'true');
    if (cursor) this.render();
    try {
      const data = await wb('global-search' + query({...options, cursor}), {signal: controller.signal});
      if (sequence !== this.sequence || controller !== this.controller) return;
      this.items = cursor ? [...this.items, ...data.items] : data.items;
      this.cursor = data.next_cursor; this.error = null;
    } catch (error) {
      if (sequence !== this.sequence || controller !== this.controller) return;
      if (!controller.signal.aborted || controller.signal.reason === 'timeout') this.error = controller.signal.reason === 'timeout' ? '搜索超时，请缩短关键词或稍后重试。' : error.message;
    } finally {
      clearTimeout(timeout);
      if (sequence === this.sequence && controller === this.controller) { this.loading = false; this.container.setAttribute('aria-busy', 'false'); this.render(); }
    }
  }
  render() {
    const focused = this.container.contains(document.activeElement) ? document.activeElement.dataset.searchKey : null;
    const nodes = [el('p', {class: 'global-search-summary', role: 'status'},
      `${({all:'全部对话（含归档）',active:'未归档对话',archived:'已归档对话'})[this.options.archive]} · 已显示 ${this.items.length} 条结果${this.cursor ? '，还有更多' : ''}`)];
    for (const item of this.items) {
      const key = item.conversation_id + ':' + (item.message_key || 'title');
      const hit = el('button', {type: 'button', class: 'global-search-hit', 'data-search-key': key,
        'aria-label': `${item.conversation_name} · ${roles[item.role] || '消息'} · ${item.snippet}`,
        onclick: async () => { try { await openResult(item); this.onOpen(item); } catch (error) { this.error = error.message; this.render(); } }},
      el('span', {class: 'global-search-heading'}, el('strong', {}, item.conversation_name),
        item.archived ? el('span', {class: 'global-search-archive'}, '已归档 · 只读') : null),
      el('span', {class: 'global-search-meta'}, roles[item.role] || '消息', ' · ', stamp(item.timestamp)),
      el('span', {class: 'global-search-snippet'}, highlighted(item.snippet, item.match)));
      nodes.push(hit);
    }
    if (this.error) nodes.push(message(this.error), button('重试搜索', () => this.load(this.cursor)));
    else if (!this.items.length && !this.loading) nodes.push(el('div', {class: 'global-search-empty'}, '没有匹配的对话或消息，试试其他关键词。'));
    if (this.cursor && !this.error) {
      const more = button(this.loading ? '正在加载…' : '加载更多结果', () => this.load(this.cursor));
      more.disabled = this.loading; more.dataset.searchKey = 'more'; nodes.push(more);
    }
    this.container.replaceChildren(...nodes);
    if (focused) [...this.container.querySelectorAll('[data-search-key]')].find(node => node.dataset.searchKey === focused)?.focus({preventScroll: true});
  }
}

let searchDialog, lastQuery = '', lastFilters = {archive: 'all', kind: 'all'};
export function openGlobalSearch(initialQuery) {
  if (searchDialog?.open) { searchDialog.querySelector('input').focus(); return; }
  if (initialQuery !== undefined) lastQuery = initialQuery;
  const input = el('input', {type: 'search', placeholder: '搜索全部对话名称和消息内容…', 'aria-label': '全局搜索关键词', maxlength: '200'});
  input.value = lastQuery;
  const archive = el('select', {'aria-label': '搜索对话范围'}, [['all','全部（含归档）'],['active','未归档'],['archived','仅归档']].map(([value, label]) => el('option', {value}, label)));
  const kind = el('select', {'aria-label': '搜索内容类型'}, [['all','名称与全部消息'],['title','仅对话名称'],['user','仅用户消息'],['assistant','仅 AI 回复'],['tool','仅工具结果']].map(([value, label]) => el('option', {value}, label)));
  archive.value = lastFilters.archive; kind.value = lastFilters.kind;
  const results = el('div', {class: 'global-search-results'});
  const close = button('×', () => dialogNode.close()); close.className = 'global-search-close'; close.setAttribute('aria-label', '关闭全局搜索');
  const dialogNode = searchDialog = el('dialog', {id: 'global-search-dialog', 'aria-labelledby': 'global-search-title'},
    el('header', {}, el('h2', {id: 'global-search-title'}, '全局搜索'), close),
    el('div', {class: 'global-search-controls'}, input, el('div', {class: 'global-search-filters'}, archive, kind)), results);
  const view = new ConversationSearch(results, {onOpen: () => dialogNode.close()});
  const run = (immediate = false) => { lastQuery = input.value; lastFilters = {archive: archive.value, kind: kind.value}; view.search(lastQuery, lastFilters, immediate); };
  input.addEventListener('input', event => { if (!event.isComposing) run(); });
  input.addEventListener('compositionend', () => run());
  input.addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'Enter') { event.preventDefault(); clearTimeout(view.timer); view.load(); }
    if (event.key === 'ArrowDown') { event.preventDefault(); results.querySelector('button')?.focus(); }
  });
  archive.onchange = kind.onchange = () => run(true);
  const returnFocus = document.activeElement;
  dialogNode.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !event.isComposing) { event.preventDefault(); event.stopPropagation(); dialogNode.close(); }
  }, true);
  dialogNode.addEventListener('close', () => { view.reset(); dialogNode.remove(); if(searchDialog===dialogNode){searchDialog=null;returnFocus?.focus({preventScroll: true});} }, {once: true});
  document.body.append(dialogNode); dialogNode.showModal(); input.focus(); run(true);
}
window.addEventListener('xgent-auth-expired', () => { searchDialog?.close(); lastQuery = ''; lastFilters = {archive: 'all', kind: 'all'}; });
