/* Conversation navigation is independent of optional workbench pages. */
const $ = id => document.getElementById(id);
const sidebar = $('wb-nav');
const list = $('conversation-list');
const scroll = $('conversation-scroll');
const search = $('conversation-search');
const menu = $('conversation-item-menu');
const renameDialog = $('conversation-rename-dialog');
const mobile = matchMedia('(max-width: 767px)');
const rows = new Map();
let itemIndex = new Map();
const paths = {
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  rename: 'm15 4 5 5M4 20l5-1L21 7a2 2 0 0 0-5-5L4 14z',
  archive: 'M4 8h16v12H4zM3 4h18v4H3zM10 12h4',
  restore: 'M4 8h16v12H4zM3 4h18v4H3zM12 17v-6m-3 3 3-3 3 3',
  close: 'm6 6 12 12M6 18 18 6',
};
let state = {current_chat_id: null, telegram_conversation_id: null, revision: -1, items: [], running: null};
let authenticated = false, authEpoch = 0, request = null, mutation = null;
let view = 'recent', listSignature = '', menuTarget = null, menuTrigger = null;
let drawerOpen = false, drawerReturn = null, previousInert = null;
let renameTarget = null, renameReturn = null, renameSaving = false;
let feedbackTimer = null;
const controllers = new Set();

function node(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value != null) element.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child != null) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(svg.namespaceURI, 'path');
  path.setAttribute('d', paths[name]);
  if (name === 'more') path.setAttribute('stroke-width', '3.5');
  svg.append(path);
  return svg;
}
function button(text, fn, attributes = {}) {
  const element = node('button', {type: 'button', ...attributes}, text);
  element.addEventListener('click', fn);
  return element;
}
function title(item) { return item?.display_title || item?.name || '新对话'; }
function timestamp(item) { return Number(item.activity_at ?? item.last_active ?? item.created_at) || 0; }
function itemById(id) { return itemIndex.get(id); }
function isArchived(item) { return item.archived === true || Number(item.archived) === 1; }
function groupFor(item) {
  const now = new Date(), date = new Date(timestamp(item) * 1000);
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const days = Math.round((today - day) / 86400000);
  return days <= 0 ? '今天' : days === 1 ? '昨天' : days < 7 ? '过去 7 天' : days < 30 ? '过去 30 天' : '更早';
}

async function api(resource, data) {
  const controller = new AbortController(), epoch = authEpoch;
  controllers.add(controller);
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
  try {
    const response = await fetch(resource, {
      credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      method: data === undefined ? 'GET' : 'POST',
      headers: {'Content-Type': 'application/json'}, body: data === undefined ? undefined : JSON.stringify(data),
    });
    let payload = {};
    try { payload = await response.json(); } catch { /* Report the HTTP failure below. */ }
    if (epoch !== authEpoch) throw new DOMException('登录状态已改变', 'AbortError');
    if (!response.ok || payload.ok === false) {
      if (response.status === 401 && authenticated) {
        window.dispatchEvent(new Event('xgent-auth-expired'));
        throw new DOMException('登录已失效', 'AbortError');
      }
      const error = new Error(payload.error || '请求失败（' + response.status + '）');
      error.status = response.status;
      throw error;
    }
    return payload;
  } catch (error) {
    if (timedOut) throw new Error('请求超时，请重试。');
    throw error;
  } finally { clearTimeout(timer); controllers.delete(controller); }
}

function accept(next) {
  if (!next?.current_chat_id || !Array.isArray(next.items) || Number(next.revision) < state.revision) return;
  $('conversation-sync-error').hidden = true;
  const previous = state.current_chat_id;
  const combined = {...state, ...next};
  if (JSON.stringify(combined) === JSON.stringify(state)) return;
  state = combined;
  itemIndex = new Map(state.items.map(item => [item.id, item]));
  render();
  window.dispatchEvent(new CustomEvent('xgent-conversation-state', {detail: state}));
  if (previous !== state.current_chat_id) {
    window.dispatchEvent(new CustomEvent('xgent-conversation-changed', {
      detail: {previous, conversation_id: state.current_chat_id},
    }));
  }
}
async function refresh() {
  if (request) return request;
  const pending = api('/api/workbench/conversations').then(accept).catch(error => {
    if (error.name === 'AbortError' || error.status === 401) return;
    if (!state.current_chat_id) renderLoadError(error);
    else {
      const notice = $('conversation-sync-error');
      notice.replaceChildren(node('span', {}, '同步暂停，保留已加载的对话。'), button('重试', refresh));
      notice.hidden = false;
    }
  }).finally(() => { if (request === pending) request = null; });
  request = pending;
  return pending;
}
function renderLoadError(error) {
  list.setAttribute('aria-busy', 'false');
  list.replaceChildren(node('div', {class: 'conv-list-empty'},
    node('strong', {}, '对话列表暂时不可用'), node('p', {}, error.message), button('重新加载', refresh)));
  listSignature = '';
}
async function mutate(name, data = {}) {
  if (mutation) return null;
  mutation = {name, id: data.id};
  render();
  try {
    const result = await api('/api/workbench/conversations/' + name, data);
    accept(result);
    return result;
  } finally { mutation = null; render(); }
}
async function prepareChat() {
  if (window.XGentWorkbench) return window.XGentWorkbench.prepareChat();
  document.body.dataset.page = 'chat';
  location.hash = '/chat';
  return true;
}
async function selectConversation(id) {
  if (mutation || !await prepareChat()) return;
  try {
    if (id !== state.current_chat_id && !await mutate('switch', {id})) return;
    closeMenu(false);
    setDrawer(false);
  } catch (error) { if (error.name !== 'AbortError') notify('切换失败：' + error.message, {error: true, retry: () => selectConversation(id)}); }
}
async function createConversation() {
  if (mutation || !state.current_chat_id || !await prepareChat()) return;
  try {
    view = 'recent'; search.value = '';
    if (!await mutate('create')) return;
    scroll.scrollTop = 0;
    closeMenu(false); setDrawer(false, false);
    requestAnimationFrame(() => $('input')?.focus({preventScroll: true}));
  } catch (error) { if (error.name !== 'AbortError') notify('新建失败：' + error.message, {error: true, retry: createConversation}); }
}
async function archiveConversation(id) {
  const item = itemById(id), wasCurrent = state.current_chat_id === id;
  if (!item || mutation) return;
  closeMenu(false);
  try {
    const result = await mutate('archive', {id});
    if (!result) return;
    const fallback = result.current_chat_id;
    notify('已归档「' + title(item) + '」', {label: '撤销', undo: async () => {
      if (!await mutate('restore', {id})) return;
      if (wasCurrent && state.current_chat_id === fallback) await mutate('switch', {id});
      notify('已恢复对话');
    }});
  } catch (error) { if (error.name !== 'AbortError') notify('归档失败：' + error.message, {error: true, retry: () => archiveConversation(id)}); }
}
async function restoreConversation(id, open = false) {
  if (mutation || (open && !await prepareChat())) return;
  closeMenu(false);
  try {
    if (!await mutate('restore', {id})) return;
    if (open) { view = 'recent'; search.value = ''; await selectConversation(id); render(); }
    else notify('已恢复到最近对话');
  } catch (error) { if (error.name !== 'AbortError') notify('恢复失败：' + error.message, {error: true, retry: () => restoreConversation(id, open)}); }
}
function setView(next) {
  view = next; search.value = ''; closeMenu(false); render();
  scroll.scrollTop = 0; search.focus({preventScroll: true});
}
function notify(text, options = {}) {
  clearTimeout(feedbackTimer);
  const feedback = $('conversation-feedback');
  const message = node('span', {class: 'conv-feedback-text'}, text);
  feedback.classList.toggle('is-error', !!options.error);
  feedback.replaceChildren(message);
  const fn = options.undo || options.retry;
  if (fn) feedback.append(button(options.label || '重试', async event => {
    event.currentTarget.disabled = true;
    try { await fn(); if (!options.undo) feedback.hidden = true; }
    catch (error) { notify(error.message, {error: true}); }
  }, {class: 'conv-feedback-action'}));
  feedback.append(button(icon('close'), () => { feedback.hidden = true; }, {class: 'conv-icon-button', 'aria-label': '关闭提示'}));
  feedback.hidden = false;
  if (!options.error) feedbackTimer = setTimeout(() => { feedback.hidden = true; }, options.undo ? 10000 : 4500);
}

function createRow(item) {
  const row = node('div', {class: 'conv-row', 'data-conversation-id': item.id});
  const name = node('span', {class: 'conv-row-title'});
  const bot = node('span', {class: 'conv-bot-badge', hidden: '', title: 'Telegram 当前使用此对话，不代表正在执行任务'}, 'Bot 当前');
  const dot = node('span', {class: 'conv-running-dot', 'aria-label': '正在生成', hidden: ''});
  const open = button([name, bot, dot], () => {
    const current = itemById(item.id);
    if (current && isArchived(current)) restoreConversation(item.id, true);
    else selectConversation(item.id);
  }, {class: 'conv-open', 'data-focus': 'open:' + item.id});
  const more = button(icon('more'), event => openMenu(item.id, event.currentTarget), {
    class: 'conv-more conv-icon-button', 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'data-focus': 'more:' + item.id,
  });
  more.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown') { event.preventDefault(); openMenu(item.id, more); }
  });
  const restore = button(icon('restore'), () => restoreConversation(item.id), {class: 'conv-restore conv-icon-button', hidden: ''});
  row.append(open, restore, more);
  return {row, open, name, more, dot, restore, bot};
}
function renderList() {
  const query = search.value.trim().toLocaleLowerCase();
  const visible = state.items.filter(item => isArchived(item) === (view === 'archived') && title(item).toLocaleLowerCase().includes(query))
    .sort((a, b) => timestamp(b) - timestamp(a) || a.id.localeCompare(b.id));
  const signature = JSON.stringify([view, query, state.current_chat_id, state.telegram_conversation_id, state.running?.conversation_id, mutation,
    visible.map(item => [item.id, title(item), groupFor(item)])]);
  if (signature === listSignature) return;
  listSignature = signature;
  const oldScroll = scroll.scrollTop;
  const focused = list.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
  const children = [];
  let group = '';
  for (const item of visible) {
    const next = query ? '搜索结果 · ' + visible.length : view === 'archived' ? '已归档的对话' : groupFor(item);
    if (next !== group) { group = next; children.push(node('h3', {class: 'conv-date-group'}, group)); }
    const entry = rows.get(item.id) || createRow(item);
    rows.set(item.id, entry);
    const current = item.id === state.current_chat_id;
    entry.row.classList.toggle('is-current', current);
    entry.row.classList.toggle('is-archived', isArchived(item));
    entry.row.classList.toggle('is-pending', mutation?.name === 'switch' && mutation.id === item.id);
    entry.bot.hidden = item.id !== state.telegram_conversation_id;
    entry.name.textContent = title(item);
    entry.open.title = title(item);
    entry.open.setAttribute('aria-label', (isArchived(item) ? '已归档：' : '打开对话：') + title(item));
    if (current) entry.open.setAttribute('aria-current', 'true'); else entry.open.removeAttribute('aria-current');
    entry.dot.hidden = state.running?.conversation_id !== item.id;
    entry.open.disabled = !!mutation || isArchived(item);
    entry.restore.hidden = !isArchived(item);
    entry.restore.disabled = !!mutation;
    entry.restore.title = '恢复对话';
    entry.restore.setAttribute('aria-label', '恢复对话：' + title(item));
    entry.more.disabled = !!mutation;
    entry.more.title = '更多操作';
    entry.more.setAttribute('aria-label', '更多操作：' + title(item));
    entry.more.setAttribute('aria-expanded', String(menuTarget === item.id));
    children.push(entry.row);
  }
  if (!visible.length) {
    children.push(node('div', {class: 'conv-list-empty'},
      node('strong', {}, query ? '没有找到对话' : view === 'archived' ? '没有已归档的对话' : '从一段新对话开始'),
      node('p', {}, query ? '换个关键词，或清除搜索。' : view === 'archived' ? '归档的对话会显示在这里。' : '你的对话会保留在这里，随时回来继续。'),
      query ? button('清除搜索', () => { search.value = ''; render(); search.focus(); }) : null));
  }
  list.replaceChildren(...children);
  list.setAttribute('aria-busy', 'false');
  scroll.scrollTop = oldScroll;
  for (const [id] of rows) if (!itemById(id)) rows.delete(id);
  if (focused) Array.from(list.querySelectorAll('[data-focus]')).find(element => element.dataset.focus === focused)?.focus({preventScroll: true});
  if (menuTarget) { if (!itemById(menuTarget)) closeMenu(false); else positionMenu(); }
}
function renderRunning() {
  const running = state.running, banner = $('conversation-running');
  $('conversation-current-status').hidden = !running || running.conversation_id !== state.current_chat_id;
  banner.hidden = !running || running.conversation_id === state.current_chat_id;
  if (banner.hidden) { banner.replaceChildren(); return; }
  const item = itemById(running.conversation_id);
  const text = node('span', {}, '「' + (item ? title(item) : running.name || '另一段对话') + '」' + (running.stop_requested ? '正在停止…' : '正在生成'));
  const visit = button('查看', () => {
    if (item && isArchived(item)) { view = 'archived'; render(); setDrawer(true); }
    else selectConversation(running.conversation_id);
  });
  visit.disabled = !item;
  const stop = button('停止', async event => {
    const trigger = event.currentTarget;
    trigger.disabled = true;
    try { await api('/api/stop', {run_id: running.run_id, conversation_id: running.conversation_id}); await refresh(); }
    catch (error) { if (error.name !== 'AbortError') notify(error.message, {error: true}); }
    finally { if (trigger.isConnected) trigger.disabled = false; }
  });
  banner.replaceChildren(node('span', {class: 'conv-running-dot', 'aria-hidden': 'true'}), text, visit, stop);
}
function render() {
  const selected = itemById(state.current_chat_id);
  $('wb-conversation-title').textContent = title(selected);
  $('wb-conversation-title').title = title(selected);
  $('conversation-new').disabled = !state.current_chat_id || !!mutation;
  $('conversation-new').setAttribute('aria-busy', String(mutation?.name === 'create'));
  $('conversation-search-clear').hidden = !search.value;
  $('conversation-list-title').textContent = view === 'archived' ? '已归档' : '最近对话';
  $('conversation-view-back').hidden = view !== 'archived';
  $('conversation-archive-view').setAttribute('aria-pressed', String(view === 'archived'));
  const count = state.items.filter(isArchived).length;
  $('conversation-archive-count').textContent = String(count);
  $('conversation-archive-count').hidden = !count;
  if (state.current_chat_id) renderList();
  renderRunning();
}

function closeMenu(restore = true) {
  const trigger = menuTrigger;
  if (trigger) trigger.setAttribute('aria-expanded', 'false');
  menu.hidden = true; menuTarget = null; menuTrigger = null;
  if (restore && trigger?.isConnected) trigger.focus({preventScroll: true});
}
function positionMenu() {
  if (!menuTrigger?.isConnected || menu.hidden) return;
  const anchor = menuTrigger.getBoundingClientRect(), bounds = menu.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(innerWidth - bounds.width - 8, anchor.right - bounds.width)) + 'px';
  menu.style.top = Math.max(8, Math.min(innerHeight - bounds.height - 8, anchor.bottom + 5)) + 'px';
}
function openMenu(id, trigger) {
  if (mutation) return;
  if (menuTarget === id) { closeMenu(); return; }
  closeMenu(false);
  const item = itemById(id);
  if (!item) return;
  menuTarget = id; menuTrigger = trigger;
  trigger.setAttribute('aria-expanded', 'true');
  const rename = button([icon('rename'), node('span', {}, '重命名')], () => openRename(id), {role: 'menuitem'});
  const other = isArchived(item)
    ? button([icon('restore'), node('span', {}, '恢复对话')], () => restoreConversation(id), {role: 'menuitem'})
    : button([icon('archive'), node('span', {}, '归档对话')], () => archiveConversation(id), {role: 'menuitem'});
  const reset=button('🧹 重置上下文',()=>confirmConversationAction('reset_context',id),{role:'menuitem'});
  const remove=button('🗑 删除对话',()=>confirmConversationAction('delete',id),{role:'menuitem',class:'danger'});
  menu.replaceChildren(rename, other, reset, remove); menu.hidden = false; positionMenu();
  rename.focus({preventScroll: true});
}
async function confirmConversationAction(action,id) {
  closeMenu(false);
  const item=itemById(id);if(!item)return;
  let info;
  try{info=await api('/api/workbench/conversations/delete_info?id='+encodeURIComponent(id));}
  catch(error){notify(error.message);return;}
  const deleting=action==='delete', dialog=node('dialog',{class:'conv-dialog conv-action-dialog','aria-label':deleting?'删除对话':'重置上下文'});
  const feedback=node('p',{role:'alert'});
  const cancel=button('取消',()=>dialog.close());
  const confirm=button(deleting?'永久删除':'确认重置',async()=>{
    cancel.disabled=confirm.disabled=true;feedback.textContent='';
    try{
      const result=await mutate(action,{id,confirm:true});if(!result)return;
      dialog.close();notify(deleting?(result.deleting?'删除已提交，正在停止所属任务…':'已删除「'+title(item)+'」'):'已重置「'+title(item)+'」的上下文，历史保留。');
      if(deleting)window.dispatchEvent(new CustomEvent('xgent-conversation-deleted',{detail:{conversation_id:id}}));
    }catch(error){feedback.textContent=error.message;}
    finally{cancel.disabled=confirm.disabled=false;}
  },{class:deleting?'danger':''});
  dialog.append(node('h2',{},deleting?'永久删除对话？':'重置模型上下文？'),node('p',{},title(item)+' · '+id.slice(0,6)),
    node('p',{},deleting?'会话历史与任务记录将永久删除，并终止 '+info.active_tasks+' 个未完成任务。共享工作目录文件和用量统计保留。':'历史消息、附件与定时任务保留；此前内容不再提供给模型，其他会话不受影响。'),
    feedback,node('div',{class:'conv-dialog-actions'},cancel,confirm));
  dialog.addEventListener('cancel',event=>{if(confirm.disabled)event.preventDefault();});
  dialog.addEventListener('close',()=>dialog.remove(),{once:true});document.body.append(dialog);dialog.showModal();cancel.focus();
}

function openRename(id) {
  const item = itemById(id);
  if (!item || mutation) return;
  renameTarget = id; renameReturn = menuTrigger || document.activeElement;
  closeMenu(false);
  $('conversation-rename-input').value = title(item);
  $('conversation-rename-error').hidden = true;
  renameDialog.showModal();
  $('conversation-rename-input').focus(); $('conversation-rename-input').select();
}
function closeRename() { if (!renameSaving) renameDialog.close(); }
renameDialog.addEventListener('close', () => {
  renameTarget = null;
  if (renameReturn?.isConnected) renameReturn.focus({preventScroll: true});
});
renameDialog.addEventListener('cancel', event => { if (renameSaving) event.preventDefault(); });
$('conversation-rename-close').addEventListener('click', closeRename);
$('conversation-rename-cancel').addEventListener('click', closeRename);
$('conversation-rename-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (renameSaving || mutation || !renameTarget) return;
  const name = $('conversation-rename-input').value.trim(), id = renameTarget;
  const error = $('conversation-rename-error');
  if (!name) { error.textContent = '请输入对话名称。'; error.hidden = false; return; }
  renameSaving = true; error.hidden = true;
  renameDialog.querySelectorAll('button,input').forEach(element => { element.disabled = true; });
  $('conversation-rename-save').textContent = '保存中…';
  try { if (await mutate('rename', {id, name})) renameDialog.close(); }
  catch (exc) { if (exc.name !== 'AbortError') { error.textContent = exc.message; error.hidden = false; } }
  finally {
    renameSaving = false;
    renameDialog.querySelectorAll('button,input').forEach(element => { element.disabled = false; });
    $('conversation-rename-save').textContent = '保存';
    if (renameDialog.open) $('conversation-rename-input').focus();
  }
});

function setDrawer(open, restoreFocus = true) {
  open = !!open && mobile.matches;
  if (open === drawerOpen) { syncConversationToggle(); return; }
  drawerOpen = open;
  document.body.classList.toggle('conv-drawer-open', open);
  $('conversation-backdrop').hidden = !open;
  syncConversationToggle();
  const shell = $('wb-shell'), bottom = $('wb-mobile-nav');
  if (open) {
    drawerReturn = document.activeElement;
    previousInert = [shell.inert, bottom.inert]; shell.inert = bottom.inert = true;
    sidebar.setAttribute('role', 'dialog'); sidebar.setAttribute('aria-modal', 'true');
    requestAnimationFrame(() => $('conversation-drawer-close').focus({preventScroll: true}));
  } else {
    closeMenu(false);
    sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal');
    if (previousInert) { [shell.inert, bottom.inert] = previousInert; previousInert = null; }
    if (restoreFocus && drawerReturn?.isConnected) drawerReturn.focus({preventScroll: true});
  }
}
$('conversation-new').addEventListener('click', createConversation);
$('conversation-drawer-toggle').addEventListener('click', () => {
  if(mobile.matches)setDrawer(!drawerOpen);
  else {const collapsed=document.body.classList.toggle('conv-sidebar-collapsed');try{localStorage.setItem('xgent-conversation-sidebar-collapsed',collapsed?'1':'0');}catch{}syncConversationToggle();}
});
$('conversation-drawer-close').addEventListener('click', () => setDrawer(false));
$('conversation-backdrop').addEventListener('click', () => setDrawer(false));
$('conversation-archive-view').addEventListener('click', () => setView(view === 'archived' ? 'recent' : 'archived'));
$('conversation-view-back').addEventListener('click', () => setView('recent'));
search.addEventListener('input', () => { closeMenu(false); render(); scroll.scrollTop = 0; });
$('conversation-search-clear').addEventListener('click', () => { search.value = ''; render(); search.focus(); });
search.addEventListener('keydown', event => {
  if (event.key === 'ArrowDown') { event.preventDefault(); list.querySelector('.conv-open:not(:disabled),.conv-restore:not([hidden])')?.focus(); }
});
list.addEventListener('keydown', event => {
  if (!event.target.matches('.conv-open') || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const choices = Array.from(list.querySelectorAll('.conv-open:not(:disabled)'));
  const index = choices.indexOf(event.target);
  if (event.key === 'ArrowUp' && index === 0) { search.focus(); return; }
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? choices.length - 1 : Math.max(0, Math.min(choices.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)));
  choices[next]?.focus();
});
scroll.addEventListener('scroll', () => {
  if (!menuTrigger || menu.hidden) return;
  const anchor = menuTrigger.getBoundingClientRect(), viewport = scroll.getBoundingClientRect();
  if (anchor.bottom < viewport.top || anchor.top > viewport.bottom) closeMenu(false);
  else positionMenu();
}, {passive: true});
window.addEventListener('resize', () => { if (!mobile.matches) setDrawer(false, false); positionMenu(); });
window.addEventListener('hashchange', () => { closeMenu(false); setDrawer(false, false); });
document.addEventListener('pointerdown', event => {
  if (!menu.hidden && !menu.contains(event.target) && !event.target.closest('.conv-more')) closeMenu(false);
}, true);
document.addEventListener('keydown', event => {
  if (event.isComposing || renameDialog.open) return;
  if (!menu.hidden) {
    const choices = Array.from(menu.querySelectorAll('button'));
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      let index = choices.indexOf(document.activeElement);
      index = event.key === 'Home' ? 0 : event.key === 'End' ? choices.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length;
      choices[index].focus(); return;
    }
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); closeMenu(); return; }
    if (event.key === 'Tab') closeMenu();
  }
  if (drawerOpen && event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); setDrawer(false); return; }
  if (drawerOpen && event.key === 'Tab') {
    const focusable = [sidebar, $('conversation-feedback')].flatMap(root => Array.from(root.querySelectorAll('button,input,a[href]'))).filter(element => !element.disabled && element.getClientRects().length);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if ((event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last)) {
      event.preventDefault(); (event.shiftKey ? last : first)?.focus();
    }
  }
}, true);

window.XGentConversations = {
  accept, refresh, get current() { return state.current_chat_id; }, get running() { return state.running; },
};
function start() { authenticated = true; refresh(); }
window.addEventListener('xgent-authenticated', start);
window.addEventListener('xgent-auth-expired', () => {
  authenticated = false; authEpoch++; controllers.forEach(controller => controller.abort()); request = null;
  setDrawer(false, false); closeMenu(false); renameDialog.close();
  clearTimeout(feedbackTimer); $('conversation-feedback').hidden = true;
  $('conversation-feedback').replaceChildren();
  $('conversation-rename-input').value = '';
  $('conversation-sync-error').hidden = true;
  state = {current_chat_id: null, revision: -1, items: [], running: null};
  rows.clear(); itemIndex.clear(); listSignature = ''; view = 'recent'; search.value = '';
  list.replaceChildren(); render();
});
api('/api/session').then(data => { if (data.authenticated) start(); }).catch(() => {});
setInterval(() => { if (authenticated && !document.hidden) refresh(); }, 3000);
document.addEventListener('visibilitychange', () => { if (authenticated && !document.hidden) refresh(); });

function syncConversationToggle(){
  $('conversation-drawer-toggle').setAttribute('aria-expanded',String(mobile.matches?drawerOpen:!document.body.classList.contains('conv-sidebar-collapsed')));
}
try{document.body.classList.toggle('conv-sidebar-collapsed',localStorage.getItem('xgent-conversation-sidebar-collapsed')==='1');}catch{}
syncConversationToggle();
mobile.addEventListener('change',syncConversationToggle);
