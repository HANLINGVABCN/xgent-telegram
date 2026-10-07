
var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) { try { tg.ready(); tg.expand(); } catch (e) {} }

var term = null, fit = null, sessionId = null, currentPid = null, es = null;
var currentFontSize = window.innerWidth < 520 ? 12 : 13.5;

function $(id) { return document.getElementById(id); }

function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  return fetch(path, Object.assign({ credentials: 'same-origin' }, opts)).then(function (r) {
    if (!r.ok) {
      return r.json().then(function (j) { throw new Error(j.error || ('HTTP ' + r.status)); });
    }
    return r.json();
  });
}

function b64ToBytes(b64) {
  var bin = atob(b64); var a = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
  return a;
}
function bytesToB64(bytes) {
  var s = ''; for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}

function toast(msg) {
  var el = $('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(function () { el.classList.remove('show'); }, 1700);
}

function copyText(text) {
  text = String(text == null ? '' : text);
  return new Promise(function (resolve, reject) {
    function legacy() {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;padding:0;border:0;opacity:0;';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        ta.setSelectionRange(0, ta.value.length);
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        ok ? resolve() : reject(new Error('execCommand copy failed'));
      } catch (e) { reject(e); }
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(resolve, legacy);
    } else {
      legacy();
    }
  });
}

function bufferText() {
  if (!term) return '';
  var buf = term.buffer.active, lines = [], i, line;
  for (i = 0; i < buf.length; i++) {
    line = buf.getLine(i);
    lines.push(line ? line.translateToString(true) : '');
  }
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  return lines.join('\n');
}

function flashStatus(msg) {
  var el = $('status');
  el.classList.add('flash');
  el._msg = msg;
  updateStatus();
  clearTimeout(el._t);
  el._t = setTimeout(function () { el.classList.remove('flash'); el._msg = null; updateStatus(); }, 1600);
}

function doCopy(text, quiet) {
  if (!text) { if (!quiet) toast('没有可复制的内容'); return; }
  copyText(text).then(function () {
    flashStatus('已复制 ' + text.length + ' 字符');
    if (!quiet) {
      var b = $('copyBtn');
      b.classList.add('done');
      b.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><use href="#i-check"/></svg>';
      clearTimeout(b._t);
      b._t = setTimeout(function () {
        b.classList.remove('done');
        b.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><use href="#i-copy"/></svg>';
      }, 1400);
    }
  }).catch(function () {
    if (!quiet) toast('复制失败，请长按选中后手动复制');
  });
}

function sendToTerm(text) {
  if (!text || !sessionId || !term) return;
  term.paste(text);
  term.focus();
}

function doPaste() {
  if (navigator.clipboard && navigator.clipboard.readText && window.isSecureContext) {
    navigator.clipboard.readText().then(function (t) {
      if (t) { sendToTerm(t); flashStatus('已粘贴 ' + t.length + ' 字符'); }
      else toast('剪贴板为空');
    }).catch(openPasteSheet);
  } else {
    openPasteSheet();
  }
}

function openPasteSheet() {
  $('pasteOverlay').style.display = 'flex';
  var box = $('pasteBox');
  box.value = '';
  setTimeout(function () { box.focus(); }, 60);
}
function closePasteSheet() { $('pasteOverlay').style.display = 'none'; if (term) term.focus(); }

$('pasteSendBtn').onclick = function () {
  var v = $('pasteBox').value;
  closePasteSheet();
  if (v) { sendToTerm(v); flashStatus('已粘贴 ' + v.length + ' 字符'); }
};
$('pasteCancelBtn').onclick = closePasteSheet;

var connState = '';
function updateStatus() {
  var el = $('status');
  var html = '<span class="conn-dot' + (connState ? ' ' + connState : '') + '"></span>';
  if (el._msg) {
    html += '<span>' + el._msg + '</span>';
  } else {
    var parts = [];
    if (currentPid) parts.push('pid ' + currentPid);
    if (term) parts.push(term.cols + '×' + term.rows);
    if (parts.length) html += '<span>' + parts.join('</span><span class="sep">·</span><span>') + '</span>';
  }
  el.innerHTML = html;
}

function setConnState(state) {
  connState = state || '';
  updateStatus();
}

function login() {
  return api('/api/session').then(function (d) {
    if (d.authenticated) return true;
    var initData = tg && tg.initData ? tg.initData : '';
    if (initData) {
      return api('/api/login', { method: 'POST', body: JSON.stringify({ init_data: initData }) })
        .then(function () { return true; });
    }
    return false;
  });
}

function showLogin() { $('loginOverlay').style.display = 'flex'; }
function hideLogin() { $('loginOverlay').style.display = 'none'; }

function startLogin() {
  login().then(function (ok) {
    if (ok) { hideLogin(); initTerm(); }
    else { showLogin(); $('pw').focus(); }
  }).catch(function () { showLogin(); });
}

$('loginBtn').onclick = function () {
  var pw = $('pw').value;
  if (!pw) { $('err').textContent = '请输入密码'; return; }
  api('/api/login', { method: 'POST', body: JSON.stringify({ password: pw }) }).then(function () {
    hideLogin(); initTerm();
  }).catch(function (e) { $('err').textContent = e.message || '登录失败'; });
};
$('pw').addEventListener('keydown', function (e) { if (e.key === 'Enter') $('loginBtn').click(); });

var outputCursor = 0;
var inputChain = Promise.resolve();
var terminalStreamGeneration=0;
function openStream() {
  if(es)es.close();
  const generation=++terminalStreamGeneration;
  const sid=sessionId;
  setConnState('connecting');
  const source=es=new EventSource('/api/term/output?session_id='+encodeURIComponent(sid)+'&after='+outputCursor);
  source.onopen=function(){if(generation===terminalStreamGeneration)setConnState('');};
  source.onmessage=function(ev){
    if(generation!==terminalStreamGeneration)return;
    try{const j=JSON.parse(ev.data);if(j.data&&term)term.write(b64ToBytes(j.data));if(ev.lastEventId)outputCursor=Number(ev.lastEventId);}catch{}
  };
  source.addEventListener('gap',function(){if(generation===terminalStreamGeneration&&term)term.write('\r\n[部分输出已超过服务器保留窗口；不能保证恢复完整历史画面]\r\n');});
  source.addEventListener('close',function(){if(generation===terminalStreamGeneration){showEnded('终端会话已退出');refreshSessions(false);}});
  source.onerror=async function(){
    if(generation!==terminalStreamGeneration)return;
    setConnState('disconnected');
    try{const session=await api('/api/session');if(generation!==terminalStreamGeneration)return;
      if(!session.authenticated){source.close();terminalStreamGeneration++;sessionId=null;sessionStorage.removeItem('xgent-terminal-session');showLogin();return;}
      const data=await api('/api/term/sessions');if(generation!==terminalStreamGeneration)return;
      if(!data.sessions.some(s=>s.id===sid&&!s.closed))showEnded('会话已失效。服务可能已重启，不会自动重新执行命令。');
    }catch{}
  };
}

var THEME = {
  background: '#08090C', foreground: '#DDE1E9',
  cursor: '#8189FF', cursorAccent: '#08090C',
  selectionBackground: 'rgba(129,137,255,.34)',
  black: '#1C1F27',   red: '#FF6B7E',     green: '#3ECF8E',  yellow: '#F0B429',
  blue: '#8189FF',    magenta: '#C08BFF', cyan: '#4CD6DE',   white: '#C6CBD6',
  brightBlack: '#5A6273', brightRed: '#FF8D9C', brightGreen: '#67E0AA', brightYellow: '#FFC94D',
  brightBlue: '#A0A6FF',  brightMagenta: '#D3A8FF', brightCyan: '#77E4EA', brightWhite: '#F2F4F8'
};

function initTerm() {
  if (term) { term.dispose(); term = null; }
  currentPid = null;
  term = new Terminal({
    fontFamily: '"SF Mono", "JetBrains Mono", "Cascadia Code", Menlo, Consolas, monospace',
    fontSize: currentFontSize,
    lineHeight: 1.25,
    letterSpacing: 0,
    cursorBlink: true,
    cursorStyle: 'bar',
    scrollback: 8000,
    rightClickSelectsWord: true,
    macOptionIsMeta: true,
    theme: THEME
  });
  fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($('term'));
  try { fit.fit(); } catch (e) {}
  updateStatus();

  term.attachCustomKeyEventHandler(function (e) {
    if (e.type !== 'keydown') return true;
    var mod = e.ctrlKey || e.metaKey;
    if (!mod) return true;
    if (e.code === 'KeyC' && (e.shiftKey || term.hasSelection())) {
      var sel = term.getSelection();
      if (sel) { doCopy(sel); e.preventDefault(); return false; }
      if (e.shiftKey) { e.preventDefault(); return false; }
    }
    if (e.code === 'KeyA' && e.shiftKey) { term.selectAll(); e.preventDefault(); return false; }
    return true;
  });

  refreshSessions(true);

  term.onData(function (data) {
    if (!sessionId) return;
    var b = new TextEncoder().encode(data);
    var target = sessionId;
    inputChain = inputChain.then(function(){return api('/api/term/input', { method: 'POST', body: JSON.stringify({ session_id: target, data: bytesToB64(b) }) });})
      .catch(function (error) { setConnState('disconnected'); if(term) term.write('\r\n[输入失败：'+error.message+']\r\n'); });
  });
  term.onResize(function (s) {
    if (!sessionId) return;
    api('/api/term/resize', { method: 'POST', body: JSON.stringify({ session_id: sessionId, cols: s.cols, rows: s.rows }) })
      .catch(function () {});
    updateStatus();
  });
}

var vkeys = document.querySelectorAll('.vkey');
Array.prototype.forEach.call(vkeys, function (k) {
  k.addEventListener('click', function () {
    var val = k.dataset.key;
    if (val) val = val.replace(/\\x([0-9a-f]{2})/gi,function(_,hex){return String.fromCharCode(parseInt(hex,16));}).replace(/\\r/g,'\r').replace(/\\n/g,'\n').replace(/\\t/g,'\t');
    if (val && term) {
      if(sessionId){var target=sessionId;var encoded=bytesToB64(new TextEncoder().encode(val));inputChain=inputChain.then(function(){return api('/api/term/input',{method:'POST',body:JSON.stringify({session_id:target,data:encoded})});}).catch(function(e){setConnState('disconnected');});}
      term.focus();
    }
  });
});

$('fontPlusBtn').onclick = function () {
  if (!term || currentFontSize >= 24) return;
  currentFontSize += 1.5;
  term.options.fontSize = currentFontSize;
  try { fit.fit(); } catch (e) {}
  updateStatus();
};
$('fontMinusBtn').onclick = function () {
  if (!term || currentFontSize <= 10) return;
  currentFontSize -= 1.5;
  term.options.fontSize = currentFontSize;
  try { fit.fit(); } catch (e) {}
  updateStatus();
};

$('term').addEventListener('mouseup', function () {
  if (!term || !term.hasSelection()) return;
  var sel = term.getSelection();
  if (sel && sel.trim()) doCopy(sel, true);
});
window.addEventListener('resize', function () { if (fit) { try { fit.fit(); } catch (e) {} } });

function showEnded(reason) {
  terminalStreamGeneration++;
  if (es) { es.close(); es = null; }
  sessionId = null;
  $('endReason').textContent = reason || '';
  var title=$('endedOverlay').querySelector('h2');if(title)title.textContent=reason&&reason.includes('不支持')?'终端不可用':reason&&reason.includes('没有运行')?'创建你的第一个终端':'会话已结束';
  $('endedOverlay').style.display = 'flex';
}

function goBackToChat() {
  if (window.parent && window.parent !== window) {
    window.parent.postMessage({ type: 'xgent-close-terminal' }, location.origin);
    return;
  }
  if (window.opener && !window.opener.closed) {
    window.opener.focus();
    window.close();
  } else {
    location.href = '/';
  }
}

$('copyBtn').onclick = function () {
  var sel = term && term.hasSelection() ? term.getSelection() : '';
  doCopy(sel && sel.trim() ? sel : bufferText());
};
$('pasteBtn').onclick = doPaste;
$('closeBtn').onclick = async function () {
  if(!sessionId||!confirm('结束这个终端会话？正在运行的命令将被终止。'))return;
  const sid=sessionId;$('closeBtn').disabled=true;
  try{await api('/api/term/close',{method:'POST',body:JSON.stringify({session_id:sid})});
    if(sessionId===sid)showEnded('已手动结束会话');sessionStorage.removeItem('xgent-terminal-session');await refreshSessions(false);
  }catch(e){$('sessionNote').textContent='关闭失败：'+e.message;}finally{$('closeBtn').disabled=false;}
};
$('backBtn').onclick = goBackToChat;
$('endedBackBtn').onclick = goBackToChat;
$('renewBtn').textContent = '创建新会话';
$('renewBtn').onclick = createSession;

startLogin();

var sessions=[];var creating=false;
async function refreshSessions(attach) {
  try {
    var data=await api('/api/term/sessions');
    sessions=data.sessions.filter(s=>!s.closed);
    var picker=$('sessionSelect');picker.replaceChildren();
    sessions.forEach(s=>{const option=document.createElement('option');option.value=s.id;option.textContent='终端 · PID '+s.pid;picker.appendChild(option);});
    $('newSessionBtn').disabled=creating||!data.supported||sessions.length>=data.max_sessions;
    $('renewBtn').disabled=!data.supported||sessions.length>=data.max_sessions;
    $('sessionNote').textContent=`${sessions.length}/${data.max_sessions} 个会话 · 收起面板不会结束命令`;
    renderTerminalTabs();
    if(!data.supported){showEnded('当前服务器平台不支持 PTY 终端（需要 Linux/Unix）。');return;}
    var saved=sessionStorage.getItem('xgent-terminal-session');
    if(attach){
      const chosen=sessions.find(s=>s.id===saved);
      if(chosen)attachSession(chosen.id,chosen.pid);
      else if(saved){showEnded('上次会话已失效，不会自动创建新 Shell。请选择已有会话或手动新建。');sessionStorage.removeItem('xgent-terminal-session');}
      else if(sessions[0])attachSession(sessions[0].id,sessions[0].pid);
      else showEnded('没有运行中的会话。创建新会话才会启动 Shell。');
    }
    if(sessionId)picker.value=sessionId;
  }catch(e){$('sessionNote').textContent='读取会话失败：'+e.message;}
}
function renderTerminalTabs(){
  const tabs=$('sessionTabs');tabs.replaceChildren();
  sessions.forEach((s,i)=>{const tab=document.createElement('button');tab.type='button';tab.textContent='终端 '+(i+1)+' · '+s.pid;
    tab.className=s.id===sessionId?'active':'';tab.setAttribute('role','tab');tab.setAttribute('aria-selected',String(s.id===sessionId));
    tab.onclick=()=>attachSession(s.id,s.pid);tabs.appendChild(tab);
  });
  const chooser=$('existingSessions');if(chooser){chooser.replaceChildren();sessions.forEach(s=>{const btn=document.createElement('button');btn.className='btn-ghost';btn.textContent='连接 PID '+s.pid;btn.onclick=()=>attachSession(s.id,s.pid);chooser.append(btn);});}
}
function attachSession(id,pid){
  if(!id)return;if(es)es.close();terminalStreamGeneration++;sessionId=id;currentPid=pid;outputCursor=0;
  sessionStorage.setItem('xgent-terminal-session',id);
  $('endedOverlay').style.display='none';if(term)term.reset();
  $('sessionSelect').value=id;renderTerminalTabs();updateStatus();openStream();if(term){fit.fit();term.focus();}
}
async function createSession(){
  if(creating)return;creating=true;$('newSessionBtn').disabled=true;$('renewBtn').disabled=true;
  try{const d=await api('/api/term/open',{method:'POST',body:JSON.stringify({cols:term?.cols||80,rows:term?.rows||24})});
    attachSession(d.session_id,d.pid);await refreshSessions(false);
  }catch(e){$('endReason').textContent='创建失败：'+e.message;$('sessionNote').textContent='创建失败：'+e.message;}
  finally{creating=false;await refreshSessions(false);}
}
$('newSessionBtn').onclick=createSession;
$('refreshSessionsBtn').onclick=function(){refreshSessions(false);};
$('sessionSelect').onchange=function(){const s=sessions.find(x=>x.id===this.value);if(s)attachSession(s.id,s.pid);};
var DARK_THEME={...THEME};
window.addEventListener('message',function(e){
  if(e.origin!==location.origin||e.data?.type!=='xgent-theme')return;
  document.body.classList.toggle('light',!e.data.dark);
  if(term)term.options.theme=e.data.dark?DARK_THEME:{...DARK_THEME,background:'#f7f9fb',foreground:'#24343e',cursor:'#147f9e'};
});
var theme=localStorage.getItem('xgent-theme');
if(theme==='light'||(!theme&&!matchMedia('(prefers-color-scheme:dark)').matches)){
  document.body.classList.add('light');THEME.background='#f7f9fb';THEME.foreground='#24343e';
}

new ResizeObserver(()=>{if(fit&&term){try{fit.fit();}catch{}}}).observe($('term-wrap'));
window.visualViewport?.addEventListener('resize',()=>{if(fit&&term){try{fit.fit();}catch{}}});
