
(function () {
  "use strict";

  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) { try { tg.ready(); tg.expand(); } catch (e) {} }

  // 触屏判定用媒体查询，不用 UA 嗅探 / 屏宽：能正确区分"窄窗口的桌面浏览器"
  // （仍然回车发送）和"接了实体键盘的平板"。触屏上回车一律换行，只有发送按钮能发。
  var IS_TOUCH = !!(window.matchMedia && window.matchMedia("(hover: none) and (pointer: coarse)").matches);

  var log = document.getElementById("log");
  var input = document.getElementById("input");
  var selectedConversationId = null;
  var conversationDrafts = Object.create(null);
  var conversationFiles = Object.create(null);
  function draftKey(cid) { return 'xgent-draft:' + (cid || selectedConversationId || 'global_memory'); }

  var btnSend = document.getElementById("btn-send");
  var btnStop = document.getElementById("btn-stop");
  var btnAttach = document.getElementById("btn-attach");
  var fileInput = document.getElementById("file-input");
  var serverUploadInput = document.getElementById('server-upload-input');
  var fileModes = new WeakMap();
  var fileBatchKeys = new WeakMap();
  var pendingFilesEl = document.getElementById("pending-files");
  var typing = document.getElementById("typing");
  var statusText = document.getElementById("status-text");
  var loginDlg = document.getElementById("login");
  var loginErr = document.getElementById("login-err");
  var loginSub = document.getElementById("login-sub");
  var settings = document.getElementById("settings");
  var settingsRows = document.getElementById("settings-rows");
  var suggest = document.getElementById("cmd-suggest");
  var toastEl = document.getElementById("toast");
  var scrollBtn = document.getElementById("scroll-bottom");
  var emptyState = document.getElementById("empty-state");
  var btnTheme = document.getElementById("btn-theme");
  var botAvatar = document.getElementById("bot-avatar");

  var byMessageId = {};
  var uiGeneration = null;
  var uiRevisions = {};
  var uiDeleted = {};
  var evtSource = null;
  var suggestIndex = -1;
  var reconnectTimer = null;
  var reconnectDelay = 1000;
  var streamWatchdog = null;
  var streamGeneration = 0;
  var streamEpoch = null;
  var streamCursor = null;
  var pollController = null;
  var liveStarted = false;
  var preferPolling = /(^|\.)trycloudflare\.com$/i.test(location.hostname);
  var msgCount = 0;
  var pendingFiles = [];
  var fileStatuses = new WeakMap();
  var uploadQueue = null;
  var queueEnabled=false,queueState=null,queueController=null,queueVersion=0,queueExpanded=false,queueRefreshPending=false;
  var queueActions=new Map(),recoveredEdits=new Set(),textSubmitting=false,textSubmission=null;
  try{
    JSON.parse(sessionStorage.getItem('xgent_queue_edits')||'[]').forEach(saved=>queueActions.set(saved.item.id,{...saved,busy:false,error:'取回结果待确认'}));
    recoveredEdits=new Set(JSON.parse(sessionStorage.getItem('xgent_queue_recovered')||'[]'));
  }catch(e){}

  function svgIcon(name, cls) {
    return '<svg class="icon' + (cls ? " " + cls : "") + '" viewBox="0 0 24 24"><use href="#i-' + name + '"/></svg>';
  }

  function triggerHaptic(type) {
    try {
      if (tg && tg.HapticFeedback) {
        if (type === "success") tg.HapticFeedback.notificationOccurred("success");
        else if (type === "warning") tg.HapticFeedback.notificationOccurred("warning");
        else tg.HapticFeedback.impactOccurred("light");
      }
    } catch (e) {}
  }

  function applyTheme(dark) {
    document.body.classList.toggle("dark", dark);
    btnTheme.innerHTML = svgIcon(dark ? "sun" : "moon");
  }
  var savedTheme = null;
  try { savedTheme = localStorage.getItem("xgent-theme"); } catch(e) {}
  if (savedTheme === "dark" || savedTheme === "light") {
    applyTheme(savedTheme === "dark");
  } else if (tg && tg.colorScheme) {
    applyTheme(tg.colorScheme === "dark");
  } else {
    applyTheme(!!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches));
  }
  btnTheme.addEventListener("click", function () {
    var isDark = !document.body.classList.contains("dark");
    applyTheme(isDark);
    try { localStorage.setItem("xgent-theme", isDark ? "dark" : "light"); } catch(e) {}
    triggerHaptic();
  });

  var COMMAND_GROUPS = [
    {
      group: "🚀 核心常用",
      items: [
        {cmd:"/start", desc:"打开主菜单与功能导航"},
        {cmd:"/skills", desc:"查看与管理 Agent 技能"},
        {cmd:"/status", desc:"查看当前状态与运行环境"},
        {cmd:"/show_chat_info", desc:"状态与记忆统计"},
        {cmd:"/clear", desc:"重置上下文，保留聊天历史"},
        {cmd:"/compress", desc:"压缩上下文并归档"}
      ]
    },
    {
      group: "🧩 技能与智能体",
      items: [
        {cmd:"/skills", desc:"管理与开关 Skill 技能列表"},
        {cmd:"/agent", desc:"开关 Agent 智能体自主模式"},
        {cmd:"/prompts", desc:"管理全局与角色提示词"}
      ]
    },
    {
      group: "⚙️ 对话参数",
      items: [
        {cmd:"/config", desc:"打开图形化设置面板"},
        {cmd:"/stream", desc:"开关流式输出模式"},
        {cmd:"/thinking", desc:"设置思考深度与推理等级"},
        {cmd:"/depth", desc:"设置全局记忆深度"},
        {cmd:"/params", desc:"调整生成温度与核采样"}
      ]
    },
    {
      group: "🤖 模型与渠道",
      items: [
        {cmd:"/models", desc:"选择默认模型"},
        {cmd:"/chat_model", desc:"选择对话主模型"},
        {cmd:"/media_model", desc:"选择媒体理解模型"},
        {cmd:"/providers", desc:"管理模型提供商"},
        {cmd:"/provider_config", desc:"导入导出渠道配置"}
      ]
    },
    {
      group: "📊 记忆与统计",
      items: [
        {cmd:"/export", desc:"导出完整记忆数据库"},
        {cmd:"/stats", desc:"Token 消耗与费用统计报表"}
      ]
    },
    {
      group: "🛠️ 系统运维",
      items: [
        {cmd:"/web", desc:"配置网页版服务与密码"},
        {cmd:"/blacklist", desc:"查看与管理命令黑名单"},
        {cmd:"/update", desc:"拉取 Git 源码热更新"},
        {cmd:"/restart", desc:"重启 XGent 服务进程"}
      ]
    }
  ];

  var FLAT_COMMANDS = [];
  COMMAND_GROUPS.forEach(function (g) { g.items.forEach(function (it) { FLAT_COMMANDS.push(it); }); });

  function api(path, options) {
    options = options || {};
    var requestPath = path;
    var cid = selectedConversationId || window.XGentConversations?.current;
    if (cid && ['history','search','tasks','artifacts'].some(name=>path.startsWith('/api/workbench/'+name))) requestPath += (path.includes('?')?'&':'?') + 'conversation_id=' + encodeURIComponent(cid);
    if (cid && options.method === 'POST' && ['/api/chat','/api/command','/api/callback','/api/stop'].includes(path)) {
      var payload = options.body ? JSON.parse(options.body) : {};
      if(path === '/api/stop') {
        var running = window.XGentConversations?.running;
        payload = {conversation_id:running?.conversation_id || cid, run_id:running?.run_id, ...payload};
      } else payload = {conversation_id:cid, ...payload};
      options.body = JSON.stringify(payload);
    }
    var notifyWorkbench = function(){
      if(options.method === 'POST' && path === '/api/config')window.dispatchEvent(new CustomEvent('xgent-workbench-change',{detail:{pages:['settings','models','usage']}}));
    };
    options.credentials = "same-origin";
    options.headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
    notifyWorkbench();
    return fetch(requestPath, options).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) { var error=new Error(body.error || ("HTTP " + r.status));error.status=r.status;throw error; }
        return body;
      });
    }).finally(notifyWorkbench);
  }

  function copyText(text) {
    text = String(text == null ? "" : text);
    return new Promise(function (resolve, reject) {
      function legacy() {
        try {
          var ta = document.createElement("textarea");
          ta.value = text;
          ta.setAttribute("readonly", "");
          ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;padding:0;border:0;opacity:0;";
          document.body.appendChild(ta);
          var sel = document.getSelection();
          var prev = sel && sel.rangeCount ? sel.getRangeAt(0) : null;
          ta.focus();
          ta.select();
          ta.setSelectionRange(0, ta.value.length);
          var ok = document.execCommand("copy");
          document.body.removeChild(ta);
          if (prev && sel) { sel.removeAllRanges(); sel.addRange(prev); }
          ok ? resolve() : reject(new Error("copy failed"));
        } catch (e) { reject(e); }
      }
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(resolve, legacy);
      } else {
        legacy();
      }
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // 高亮词法片段。用正则字面量 + .source 拼装，避免 new RegExp 里的双重转义。
  var HL_STR_RE = /"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`/;
  var HL_KWD_RE = /\b(?:def|class|return|import|from|if|elif|else|for|while|try|except|finally|async|await|with|as|lambda|yield|function|const|let|var|switch|case|break|continue|default|new|throw|typeof|instanceof|interface|type|enum|public|private|static|void|int|string|bool|SELECT|FROM|WHERE|INSERT|UPDATE|DELETE|JOIN|GROUP|ORDER|BY|LIMIT|CREATE|TABLE)\b/;
  var HL_NUM_RE = /\b\d+(?:\.\d+)?\b/;
  var HL_HASH_LANGS = /^(?:py|python|bash|sh|shell|zsh|yaml|yml|rb|ruby|toml|ini|conf|dockerfile|makefile)$/;

  function hlRegex(commentRe) {
    return new RegExp(
      "(" + HL_STR_RE.source + ")|(" + commentRe.source + ")|(" +
      HL_KWD_RE.source + ")|(" + HL_NUM_RE.source + ")", "g");
  }
  var HL_RE_HASH = hlRegex(/#[^\n]*/);
  var HL_RE_SLASH = hlRegex(/\/\/[^\n]*|\/\*[\s\S]*?\*\//);

  // 单遍扫描"原始源码"，只在拼接时对叶子文本转义。
  // 旧实现是先整体 escapeHtml、再对结果反复 replace，于是自己注入的
  // <span class="hl-str"> 会被关键字表里的 class 再匹配一次（产出
  // <span <span class="hl-kwd">class</span>="hl-str">），转义出来的 &#39;
  // 里的 39 也会被数字规则吃掉。扫原文就没有这两类自噬。
  function highlightCode(code, lang) {
    lang = (lang || "").toLowerCase().trim();
    var re = HL_HASH_LANGS.test(lang) ? HL_RE_HASH : HL_RE_SLASH;
    var out = "", last = 0, m;
    re.lastIndex = 0;   // 模块级 /g 正则跨调用复用，必须重置
    while ((m = re.exec(code)) !== null) {
      out += escapeHtml(code.slice(last, m.index));
      var cls = m[1] ? "hl-str" : m[2] ? "hl-cmt" : m[3] ? "hl-kwd" : "hl-num";
      out += '<span class="' + cls + '">' + escapeHtml(m[0]) + "</span>";
      last = m.index + m[0].length;
    }
    return out + escapeHtml(code.slice(last));
  }

  var ALLOWED = {
    B:1, STRONG:1, I:1, EM:1, U:1, INS:1, S:1, STRIKE:1, DEL:1, CODE:1, PRE:1, BR:1, A:1, BLOCKQUOTE:1,
    TABLE:1, THEAD:1, TBODY:1, TR:1, TH:1, TD:1, DIV:1, SPAN:1, P:1,
    H1:1, H2:1, H3:1, H4:1, H5:1, H6:1, UL:1, OL:1, LI:1, HR:1,
    // Telegram 原生剧透 / 自定义 emoji：渲染器当前不产出，但模型可能原样输出。
    // 收进白名单后剧透文字才不会被“拆标签”直接摊开（配合 CSS 模糊 + 点击揭示）；
    // 自定义元素在 HTML 文档里 tagName 会大写化，故键名写成 TG-SPOILER/TG-EMOJI。
    "TG-SPOILER":1, "TG-EMOJI":1
  };

  // 把 marked 的标准输出对齐到本项目既有的 CSS，省得为了一个解析器去改整套样式。
  // 三件事都必须在"剥属性"之前做完——align / style / type 随后都会被清掉。
  function normalizeMarkdownNode(c) {
    // 1) GFM 任务列表：marked 出 <input type=checkbox disabled>，本项目是 .task-checkbox
    if (c.tagName === "INPUT") {
      if (c.getAttribute("type") !== "checkbox") return false;
      // 用 hasAttribute 而不是 .checked：marked 出的是 checked 属性，
      // 而 .checked 这个 IDL 属性在非浏览器 DOM 实现里不一定跟着属性走。
      var checked = c.hasAttribute("checked");
      var box = document.createElement("span");
      box.className = "task-checkbox" + (checked ? " checked" : "");
      if (checked) box.textContent = "✓";
      var li = c.parentNode;
      c.parentNode.replaceChild(box, c);
      // 样式挂在 .task-item 上，而 marked 并不给 <li> 加 class（松散列表时
      // checkbox 还会多包一层 <p>），所以往上找到 LI 自己补。
      while (li && li.tagName !== "LI") li = li.parentNode;
      if (li && li.classList) li.classList.add("task-item");
      return true;   // 已替换，调用方不要再往下走
    }
    // 2) 任务项样式类对齐（部分 marked 版本会给 task-list-item）
    if (c.tagName === "LI" && c.classList.contains("task-list-item")) c.classList.add("task-item");
    // 3) 表格列对齐：属性会被剥掉，先落到 class 上
    if (c.tagName === "TH" || c.tagName === "TD") {
      var al = c.getAttribute("align") || (c.style && c.style.textAlign);
      if (al) c.classList.add("ta-" + al);
    }
    // 4) 宽表格的横向滚动容器。marked 只出裸 <table>，而 overflow-x 挂在
    //    .table-wrap 上，少了它宽表格会直接撑破气泡。
    if (c.tagName === "TABLE" && !(c.parentNode && c.parentNode.className === "table-wrap")) {
      var tw = document.createElement("div");
      tw.className = "table-wrap";
      c.parentNode.insertBefore(tw, c);
      tw.appendChild(c);
    }
    return false;
  }

  function sanitizeHtml(htmlStr) {
    // Parse inertly: a detached DIV can still fetch IMG URLs and fire onerror
    // before its forbidden attributes/nodes are removed. Template contents cannot.
    var tmp = document.createElement("template");
    tmp.innerHTML = String(htmlStr);
    (function clean(node) {
      Array.prototype.slice.call(node.childNodes).forEach(function (c) {
        if (c.nodeType === 3) return;
        if (c.nodeType !== 1) { node.removeChild(c); return; }
        if (/^(SCRIPT|STYLE|IFRAME|OBJECT|EMBED|SVG|MATH|TEMPLATE|LINK|META)$/.test(c.tagName)) {
          node.removeChild(c);
          return;
        }
        if (normalizeMarkdownNode(c)) return;
        if (!ALLOWED[c.tagName]) {
          clean(c);
          var frag = document.createDocumentFragment();
          while (c.firstChild) frag.appendChild(c.firstChild);
          c.parentNode.replaceChild(frag, c);
          return;
        }
        Array.prototype.slice.call(c.attributes).forEach(function (a) {
          if (c.tagName === "A" && a.name === "href") {
            if (!/^(https?:|mailto:)/i.test(a.value)) c.removeAttribute(a.name);
          } else if (c.tagName === "A" && (a.name === "target" || a.name === "rel")) {
          } else if (c.tagName === "BLOCKQUOTE" && (a.name === "expandable" || a.name === "data-raw" || a.name === "data-output-path")) {
            // 保留：服务端 render_folded_html 用 <blockquote expandable> 标记
            // 「可展开折叠的协议代码块」（与电报原生 expandable 同源），网页据此做
            // 折叠交互（CSS blockquote[expandable] + bindContentClicks 里的点击切换）。
          } else if (a.name === "class") {
            var classes = a.value.split(/\s+/).filter(function (name) {
              return /^(language-[a-zA-Z0-9_+#.-]+|ta-(left|center|right)|task-item|task-checkbox|checked|table-wrap)$/.test(name);
            });
            if (classes.length) c.setAttribute("class", classes.join(" "));
            else c.removeAttribute("class");
          } else {
            c.removeAttribute(a.name);
          }
        });
        if (c.tagName === "A") { c.target = "_blank"; c.rel = "noopener noreferrer"; }
        clean(c);
      });
    })(tmp.content);
    return tmp.innerHTML;
  }

  function htmlWithLineBreaks(htmlStr) {
    var tmp = document.createElement("div");
    tmp.innerHTML = htmlStr;
    (function visit(node) {
      if (/^(PRE|CODE)$/.test(node.tagName)) return;
      Array.prototype.slice.call(node.childNodes).forEach(function (c) {
        if (c.nodeType === 1) { visit(c); return; }
        if (c.nodeType !== 3 || c.nodeValue.indexOf("\n") < 0) return;
        if (/^(TABLE|THEAD|TBODY|TR|UL|OL)$/.test(node.tagName)) return;
        // Formatting whitespace next to a block is not another visible blank
        // paragraph. Leave the original text node intact instead of adding BRs.
        if (/^\s*$/.test(c.nodeValue) && [c.previousSibling,c.nextSibling].some(function(s){
          return s && /^(PRE|BLOCKQUOTE|DIV|TABLE|UL|OL|P|H[1-6]|HR)$/.test(s.tagName);
        })) return;
        var frag = document.createDocumentFragment();
        c.nodeValue.split("\n").forEach(function (line, i) {
          if (i) frag.appendChild(document.createElement("br"));
          frag.appendChild(document.createTextNode(line));
        });
        c.parentNode.replaceChild(frag, c);
      });
    })(tmp);
    return tmp.innerHTML;
  }

  // GFM 解析交给 marked（本地 vendor，不联网）。breaks:true 是必须的：
  // Agent / 聊天输出严重依赖单换行可见，旧渲染器把每个 \n 都转成了 <br>，
  // 用 GFM 默认的 breaks:false 会让大量既有输出塌成一段。
  if (window.marked && marked.use) {
    marked.use({ gfm: true, breaks: true });
  }

  function renderMarkdown(src) {
    if (window.marked && marked.parse) {
      try { return marked.parse(src); } catch (e) { /* 落到下面的纯文本兜底 */ }
    }
    return escapeHtml(src).replace(/\n/g, "<br>");
  }

  function renderText(text, parseMode) {
    text = text == null ? "" : String(text);
    if (parseMode && String(parseMode).toUpperCase().indexOf("HTML") >= 0) {
      return htmlWithLineBreaks(sanitizeHtml(text));
    }

    // marked 直接出 <pre><code class="language-xxx">，正好是 enhanceBubbleContent
    // 期待的形状（它按 /language-(...)/ 读语言并补外壳），所以这里不再需要
    // 旧的 %%CODE_BLOCK_n%% 占位符回填——那个用字符串做 replace 的 pattern，
    // 代码里出现 $& / $` / $' / $$ 会被当成替换模式吃掉。
    return sanitizeHtml(renderMarkdown(text));
  }

  // 用户气泡永远按字面渲染：只转义 + 换行，绝不过 marked / HTML，免得输入里的
  // *、`、#、| 之类被当 markdown 语法篡改显示（Telegram/CLI 两端都原样回显用户
  // 输入，Web 对齐）。其余角色照常走 renderText。初始渲染(makeBubble)与流式/编辑
  // 更新(putTextFrame)两处共用，避免规则失步。
  function renderBubbleBody(role, text, parseMode, webDisplay) {
    if (role === "user") {
      return escapeHtml(text == null ? "" : String(text)).replace(/\n/g, "<br>");
    }
    // Reported errors are literal diagnostics both live and in durable history.
    // Do not turn upstream Markdown, HTML or stack traces into different layouts.
    if(role==='sys'&&!parseMode&&/^⚠️(?:\s|$)/.test(String(text||''))){
      return '<pre>'+escapeHtml(String(text||''))+'</pre>';
    }
    if(webDisplay?.version===1 && Array.isArray(webDisplay.segments) &&
       webDisplay.segments.every(s=>s && ['markdown','html'].includes(s.kind) && typeof s.text==='string')){
      // Every segment uses the same allowlist. Never trust even stored/tool HTML.
      return webDisplay.segments.map(s=>renderText(s.text,s.kind==='html'?'HTML':null)).join('\n');
    }
    return renderText(text, parseMode);
  }

  function repaintBubble(entry, html) {
    var next=document.createElement('div');next.innerHTML=html;
    var oldBlocks=Array.from(entry.body.querySelectorAll('blockquote[expandable]'));
    next.querySelectorAll('blockquote[expandable]').forEach(function(block){
      var key=block.getAttribute('data-raw'),path=block.getAttribute('data-output-path');
      var text=block.querySelector('pre')?.textContent||'';
      var index=oldBlocks.findIndex(old=>key ? old.getAttribute('data-raw')===key :
        old.getAttribute('data-output-path')===path && old._pbText===text);
      if(index>=0)block.replaceWith(oldBlocks.splice(index,1)[0]);
    });
    oldBlocks.forEach(block=>{clearTimeout(block._ft);block._outputRequest?.abort();});
    var oldCode=Array.from(entry.body.querySelectorAll('.code-block-wrap'));
    next.querySelectorAll('pre').forEach(function(pre){
      if(pre.closest('blockquote[expandable],.code-block-wrap'))return;
      var code=pre.querySelector('code'),text=(code||pre).textContent;
      var lang=/language-([a-zA-Z0-9_+#.-]+)/.exec(code?.className||'')?.[1]||'CODE';
      var index=oldCode.findIndex(wrap=>wrap._codeText===text && wrap._codeLang===lang);
      if(index>=0)pre.replaceWith(oldCode.splice(index,1)[0]);
    });
    enhanceBubbleContent(next);
    entry.body.replaceChildren(...next.childNodes);
  }

  function nowTime(timestamp) {
    var d = timestamp == null ? new Date() : new Date(Number(timestamp) * 1000);
    if (isNaN(d.getTime())) d = new Date();
    return (d.getHours() < 10 ? "0" : "") + d.getHours() + ":" + (d.getMinutes() < 10 ? "0" : "") + d.getMinutes();
  }

  function toast(msg, alert) {
    if (!msg) return;
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(function () { toastEl.classList.remove("show"); }, alert ? 2600 : 1800);
  }

  var cardRequests = new Map(), cardScrollIntent = null;
  function finishCardRequest(id) {const pending=cardRequests.get(id);if(pending){clearTimeout(pending.timer);pending.button.disabled=false;pending.button.classList.remove('loading');cardRequests.delete(id);}}
  function cancelCardFollow(){cardScrollIntent=null;}
  for(const kind of ['wheel','touchstart','pointerdown'])log.addEventListener(kind,cancelCardFollow,{passive:true});
  log.addEventListener('keydown',event=>{if(['PageUp','PageDown','Home','End','ArrowUp','ArrowDown'].includes(event.key))cancelCardFollow();});
  async function finishCardScroll(frame){
    const intent=cardScrollIntent;finishCardRequest(frame.interaction_id);
    if(!intent||intent.id!==frame.interaction_id||intent.cid!==selectedConversationId)return;
    if(frame.appended){
      if(viewingPast){historyAnchor=null;viewingPast=false;newFrames=0;await resync();}
      if(cardScrollIntent===intent&&intent.cid===selectedConversationId)scrollDown();
    }else{
      const entry=byMessageId[frame.edited_message_id]||byMessageId[intent.messageId];
      if(entry&&cardScrollIntent===intent)entry.row.scrollIntoView({block:'nearest'});
    }
    if(cardScrollIntent===intent)cardScrollIntent=null;
  }
  var renderingHistory=0;
  function scrollDown() { if(renderingHistory)return;log.scrollTop = log.scrollHeight; hideScrollBtn(); }
  function isNearBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 90; }
  function hideScrollBtn() { scrollBtn.classList.remove("show"); }
  function showScrollBtn() { scrollBtn.classList.add("show"); }
  function updateEmptyState() { emptyState.classList.toggle("hidden", msgCount !== 0); }

  function flashCopied(btn, label) {
    var old = btn.innerHTML;
    btn.innerHTML = svgIcon("check") + (label ? "<span>已复制</span>" : "");
    btn.classList.add("done");
    triggerHaptic("success");
    clearTimeout(btn._t);
    btn._t = setTimeout(function () { btn.innerHTML = old; btn.classList.remove("done"); }, 1400);
  }

  function paintProtocolBody(bq) {
    var pre = bq.querySelector("pre");
    if (!pre || bq._pbText === undefined) return;
    var expanded = bq.classList.contains("expanded") || bq.classList.contains("pb-short");
    bq.querySelector(".pb-toggle")?.setAttribute("aria-expanded",String(expanded));
    var text = bq._pbText;
    if (!expanded) {
      // 收起时只构建三行 DOM；这不是内容删除，复制和展开都使用完整 _pbText。
      var end = 0;
      for (var i = 0; i < 3; i++) { end = text.indexOf("\n", end); if (end < 0) break; end++; }
      if (end >= 0) text = text.slice(0, end);
    }
    // 大正文不做语法高亮膨胀，不为每个 token 创建 span。
    if (text.length > 20000) pre.textContent = text;
    else pre.innerHTML = '<code>' + highlightCode(text, bq._pbLang || "shell") + '</code>';
  }

  function closeOutputPage(bq) {
    bq._outputVersion=(bq._outputVersion||0)+1;
    bq._outputRequest?.abort();bq._outputRequest=null;bq._outputLoading=false;
    bq.querySelector('.output-pages')?.remove();bq.classList.remove('output-open');
    var trigger=bq.querySelector('.btn-output');if(trigger){trigger.textContent='查看完整输出';trigger.setAttribute('aria-expanded','false');}
  }
  function showOutputPage(bq, offset, previous) {
    if(bq._outputLoading)return;
    bq._outputLoading=true;
    var version=bq._outputVersion=(bq._outputVersion||0)+1;
    var controller=bq._outputRequest=new AbortController();
    var area=bq.querySelector('.output-pages');
    if(!area){area=document.createElement('div');area.className='output-pages';bq.appendChild(area);}
    bq.classList.add('output-open');
    var trigger=bq.querySelector('.btn-output');if(trigger){trigger.textContent='收起完整输出';trigger.setAttribute('aria-expanded','true');}
    var loading=document.createElement('p');loading.className='output-page-label';loading.textContent='正在读取完整日志…';area.appendChild(loading);
    api('/api/output/page',{method:'POST',signal:controller.signal,body:JSON.stringify({path:bq.getAttribute('data-output-path'),offset:offset})}).then(function(page){
      if(version!==bq._outputVersion||!area.isConnected)return;
      bq._outputPage=page;bq._outputPrevious=previous;area.replaceChildren();
      var controls=document.createElement('div');controls.className='pb-head';
      var label=document.createElement('span');label.className='output-page-label';
      label.textContent=page.filename+' · '+page.offset+'–'+page.next_offset+' / '+page.size+' bytes';controls.appendChild(label);
      [['prev','上一页',!previous.length],['next','下一页',page.eof],['copy','复制本页',false],['close','收起完整输出',false]].forEach(function(item){var button=document.createElement('button');button.type='button';button.className='code-btn btn-output-'+item[0];button.textContent=item[1];button.disabled=item[2];controls.appendChild(button);});
      var pre=document.createElement('pre');pre.className='output-page-text';pre.dataset.enhanced='1';pre.textContent=page.text;
      area.appendChild(controls);area.appendChild(pre);
    }).catch(function(err){if(err.name!=='AbortError'&&version===bq._outputVersion){closeOutputPage(bq);toast('读取输出失败：'+err.message,true);}}).finally(function(){if(version===bq._outputVersion){bq._outputLoading=false;bq._outputRequest=null;}});
  }

  function enhanceProtocolBlocks(container) {
    var bqs = container.querySelectorAll("blockquote[expandable]");
    Array.prototype.forEach.call(bqs, function (bq) {
      if (bq.dataset.pb === "1") return;
      bq.dataset.pb = "1";
      var pre = bq.querySelector("pre");
      var label = bq.querySelector("i");
      var title = "";
      var node = bq.firstChild;
      while (node && node !== pre) { title += node.textContent || ""; node = node.nextSibling; }
      title = title.replace(/\s+/g, " ").trim();
      var text = pre ? (pre.querySelector("code") || pre).textContent : "";
      // 语言从 <code class="language-xxx"> 读真值：此前写死 "CODE" → 落到 // 词法，
      // 对协议块这种命令执行输出是错的。没有语言类时按 shell（# 注释）兜底，而不是
      // 通用 // 词法——协议块本就是命令上下文。必须在下面清空 bq 之前读，否则 code
      // 元素连同 class 会被 removeChild 掉。
      var pbCode = pre && pre.querySelector("code");
      var pbLang = "shell";
      if (pbCode && pbCode.className) {
        var pbLm = /language-([a-zA-Z0-9_+#.-]+)/.exec(pbCode.className);
        if (pbLm) pbLang = pbLm[1];
      }
      var short = !label && text.split("\n").length <= 3;

      var head = document.createElement("div");
      head.className = "pb-head";
      head.innerHTML = (short ? "" : '<span class="pb-caret">▶</span>') +
        '<span class="pb-title">' + (function () {
          var cut = title.lastIndexOf(" · ");
          return cut < 0 ? '<b>' + escapeHtml(title) + '</b>'
            : '<b>' + escapeHtml(title.slice(0, cut)) + '</b><span class="pb-count">' + escapeHtml(title.slice(cut)) + '</span>';
        })() + '</span>' +
        (pre ? '<button class="code-btn btn-wrap" type="button" title="切换自动换行">' + svgIcon("wrap") + '<span>换行</span></button>' +
               '<button class="code-btn btn-copy" type="button" title="复制显示的代码">' + svgIcon("copy") + '<span>复制</span></button>' : '') +
        (bq.getAttribute("data-raw") ? '<button class="code-btn btn-copy-all" type="button" title="复制整个协议块（BEGIN 到 END 原文）">' + svgIcon("copy") + '<span>复制全部</span></button>' : '');

      head.title = title;
      if(!short){var toggle=document.createElement("button");toggle.type="button";toggle.className="pb-toggle";toggle.setAttribute("aria-label","展开或收起 "+title);toggle.setAttribute("aria-expanded",String(bq.classList.contains("expanded")));var caret=head.querySelector(".pb-caret"),caption=head.querySelector(".pb-title");if(caret)toggle.appendChild(caret);toggle.appendChild(caption);head.prepend(toggle);}
      if (bq.getAttribute('data-output-path')) {
        var archive = document.createElement('button'); archive.type = 'button';
        archive.className = 'code-btn btn-output'; archive.textContent = '查看完整输出';archive.setAttribute('aria-expanded','false');
        head.appendChild(archive);
      }
      bq._pbText = text; bq._pbLang = pbLang;
      while (bq.firstChild) bq.removeChild(bq.firstChild);
      bq.appendChild(head);
      if (pre) {
        pre.dataset.enhanced = "1";
        pre.textContent = '';
        bq.appendChild(pre);
      }
      if (label) { label.className = "pb-folded"; bq.appendChild(label); }
      bq.classList.add("pb");
      if (short) bq.classList.add("pb-short");
      paintProtocolBody(bq);
    });
  }

  function enhanceBubbleContent(container) {
    enhanceProtocolBlocks(container);
    var pres = container.querySelectorAll("pre");
    Array.prototype.forEach.call(pres, function (pre) {
      if (pre.dataset.enhanced === "1") return;
      pre.dataset.enhanced = "1";

      var wrap = pre.closest(".code-block-wrap");
      if (!wrap) {
        wrap = document.createElement("div");
        wrap.className = "code-block-wrap";
        pre.parentNode.insertBefore(wrap, pre);

        var codeTag = pre.querySelector("code");
        var rawCode = codeTag ? codeTag.textContent : pre.textContent;
        var lang = "CODE";
        if (codeTag && codeTag.className) {
          var lm = /language-([a-zA-Z0-9_+#.-]+)/.exec(codeTag.className);
          if (lm) lang = lm[1];
        }
        wrap._codeText=rawCode;wrap._codeLang=lang;

        var header = document.createElement("div");
        header.className = "code-header";
        header.innerHTML = '<span class="code-lang">' + escapeHtml(lang.toUpperCase()) + '</span>' +
          '<div class="code-actions">' +
            '<button class="code-btn btn-wrap" type="button" title="切换自动换行">' + svgIcon("wrap") + '<span>换行</span></button>' +
            '<button class="code-btn btn-copy" type="button" title="复制代码">' + svgIcon("copy") + '<span>复制</span></button>' +
          '</div>';

        if (rawCode.length > 20000) pre.textContent = rawCode;
        else pre.innerHTML = '<code>' + highlightCode(rawCode, lang) + '</code>';
        wrap.appendChild(header);
        wrap.appendChild(pre);
      }
      // 点击行为统一走 log 上的事件委托（bindContentClicks），这里只负责建壳 +
      // 高亮。旧实现每次渲染都重绑一遍，而流式 edit 帧每批 token 都重建整棵子树。
    });
  }

  // 代码块按钮 + 行内代码复制，统一委托到聊天容器上。
  // 行内 <code>（含引用块 / 表格单元格里的）以前完全没有绑定，点了没反应。
  function bindContentClicks(root) {
    root.addEventListener("click", function (e) {
      if (!e.target.closest) return;

      var btn = e.target.closest(".code-btn");
      if (btn) {
        var wrap = btn.closest(".code-block-wrap, blockquote.pb");
        var pre = wrap && wrap.querySelector("pre");
        e.stopPropagation();
        if (btn.classList.contains('btn-output')) {
          if(wrap.classList.contains('output-open'))closeOutputPage(wrap);else showOutputPage(wrap,0,[]);return;
        }
        if (btn.classList.contains('btn-output-copy')) {
          if (wrap._outputPage) copyText(wrap._outputPage.text)
            .then(function () { flashCopied(btn, true); }).catch(function () { toast('复制失败'); });
          return;
        }
        if (btn.classList.contains('btn-output-close')) {
          closeOutputPage(wrap);
          wrap._outputPage = null; wrap._outputPrevious = []; return;
        }
        if (btn.classList.contains('btn-output-next')) {
          var page = wrap._outputPage;
          if (page && !page.eof) showOutputPage(wrap, page.next_offset, (wrap._outputPrevious || []).concat([page.offset]));
          return;
        }
        if (btn.classList.contains('btn-output-prev')) {
          var previous = (wrap._outputPrevious || []).slice();
          if (previous.length) showOutputPage(wrap, previous.pop(), previous);
          return;
        }
        if (btn.classList.contains("btn-copy-all")) {
          copyText(wrap.getAttribute("data-raw") || "").then(function () { flashCopied(btn, true); })
                                                      .catch(function () { toast("复制失败，请手动选中"); });
          return;
        }
        if (!pre) return;
        if (btn.classList.contains("btn-copy")) {
          var codeEl = pre.querySelector("code") || pre;
          copyText(wrap._pbText === undefined ? codeEl.textContent : wrap._pbText).then(function () { flashCopied(btn, true); })
                                      .catch(function () { toast("复制失败，请手动选中"); });
        } else if (btn.classList.contains("btn-wrap")) {
          pre.classList.toggle("wrapped");
          btn.classList.toggle("done", pre.classList.contains("wrapped"));
        }
        return;
      }

      // 协议块：单击任意处展开/收起；双击/三击（e.detail>1）取消待切换、留给选词；
      // 拖选结束的 click 因有选区也不切换。≤3 行的块（pb-short）不折叠。
      var foldBq = e.target.closest("blockquote.pb");
      if (foldBq) {
        e.stopPropagation();
        if (foldBq.classList.contains("pb-short") || e.target.closest(".pb-folded, .output-pages")) return;
        clearTimeout(foldBq._ft);
        if (e.detail > 1) return;
        foldBq._ft = setTimeout(function () {
          if (String(window.getSelection() || "").length) return;
          if(foldBq.classList.contains("output-open")){closeOutputPage(foldBq);foldBq.classList.remove("expanded");}else foldBq.classList.toggle("expanded");
          paintProtocolBody(foldBq);
        }, 250);
        return;
      }

      // 剧透块：点一下揭示（露出后不再拦截，交回下面的选词/复制逻辑）。
      var spoiler = e.target.closest("tg-spoiler");
      if (spoiler && !spoiler.classList.contains("revealed")) {
        spoiler.classList.add("revealed");
        e.stopPropagation();
        return;
      }

      var inline = e.target.closest("code");
      if (!inline || inline.closest("pre")) return;           // 大代码块交给上面的按钮
      if (String(window.getSelection() || "").length) return; // 用户在选词，不抢
      copyText(inline.textContent).then(function () {
        inline.classList.add("copied");
        triggerHaptic("success");
        clearTimeout(inline._t);
        inline._t = setTimeout(function () { inline.classList.remove("copied"); }, 1200);
      }).catch(function () { toast("复制失败，请手动选中"); });
    });
  }

  function isTokenUsageText(text) {
    return typeof text === "string" && /tokens\/s/.test(text) &&
      text.indexOf("↑") >= 0 && text.indexOf("↓") >= 0;
  }

  var AGENT_STATUS_RE = /Agent\s*第\s*\d+\s*轮/;
  var AGENT_RESULT_RE = /<b>\s*Agent\s+\w+\s*<\/b>/i;

  function classifyMessageText(role, text, messageType) {
    if (messageType === "token_usage") return "token";
    if (messageType === "agent_status" || messageType === "system_op" || messageType === "runtime_error") return "sys";
    if (messageType === "agent_result") return "cmd";
    if (messageType || role === "user") return role;
    if (typeof text !== "string") return role;
    if (/^⚠️(?:\s|$)/.test(text)) return 'sys';
    if (isTokenUsageText(text)) {
      return "token";
    }
    if (role === "ai" || role === "sys" || role === "cmd") {
      if (AGENT_RESULT_RE.test(text)) return "cmd";
      if (AGENT_STATUS_RE.test(text)) return "sys";
    }
    return role;
  }

  function speakText(text, btn) {
    if (!window.speechSynthesis) { toast("当前浏览器不支持语音朗读"); return; }
    if (window.speechSynthesis.speaking) {
      window.speechSynthesis.cancel();
      if (btn) btn.classList.remove("done");
      return;
    }
    var clean = text.replace(/<[^>]+>/g, "").replace(/https?:\/\/[^\s]+/g, "链接");
    var u = new SpeechSynthesisUtterance(clean);
    u.lang = "zh-CN";
    u.rate = 1.05;
    u.onend = function () { if (btn) btn.classList.remove("done"); };
    u.onerror = function () { if (btn) btn.classList.remove("done"); };
    if (btn) btn.classList.add("done");
    window.speechSynthesis.speak(u);
  }

  var lastStreamEntry = null;
  function makeBubble(role, text, messageId, parseMode, markup, messageType, timestamp, webDisplay) {
    var stick = isNearBottom();
    msgCount++;
    updateEmptyState();

    role = classifyMessageText(role, text, messageType);

    var row = document.createElement("div");
    row.className = "msg-row " + role;

    if (role === "token") {
      var pill = document.createElement("div");
      pill.className = "token-pill";
      pill.innerHTML = renderText(text, parseMode);
      enhanceBubbleContent(pill);
      row.appendChild(pill);
      log.appendChild(row);
      if (stick) scrollDown();
      var tEntry = { row: row, body: pill, bubble: pill, head: null, btns: null, rawText: text, role: role };
      if (messageId != null) byMessageId[messageId] = tEntry;
      return tEntry;
    }

    var bubble = document.createElement("div");
    bubble.className = "bubble " + role;

    var head = null;
    if (role === "ai" || role === "cmd" || role === "sys" || role === "user") {
      head = document.createElement("div");
      head.className = "bubble-header";
      var iconCls = role === "ai" ? "" : (" " + role);
      var iconText = role === "ai" ? "X" : "◇";
      var authorName = role === "ai" ? "XGent" : role === "user" ? "你" : (role === "cmd" ? "工具结果" : "系统");
      head.innerHTML = '<div class="bubble-author"><span class="bubble-author-icon' + iconCls + '">' + iconText + '</span> ' + authorName + '</div>';
      bubble.appendChild(head);
    }

    var body = document.createElement("div");
    body.className = "body";
    body.innerHTML = renderBubbleBody(role, text, parseMode, webDisplay);
    enhanceBubbleContent(body);

    var ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = nowTime(timestamp);
    if (timestamp != null) ts.title = new Date(Number(timestamp) * 1000).toLocaleString();

    bubble.appendChild(body);
    bubble.appendChild(ts);

    // 将快捷操作栏直接挂在气泡右上角，确保绝对对齐且目标精准
    var actBar = document.createElement("div");
    actBar.className = "msg-actions";

    var btnCp = document.createElement("button");
    btnCp.className = "act-btn";
    btnCp.type = "button";
    btnCp.title = "复制全文";
    btnCp.innerHTML = svgIcon("copy");
    btnCp.addEventListener("click", function (e) {
      e.stopPropagation();
      copyText(body.innerText).then(function () { flashCopied(btnCp, false); })
                              .catch(function () { toast("复制失败"); });
    });
    actBar.appendChild(btnCp);

    if (role === "ai" || role === "cmd" || role === "sys") {
      var btnQt = document.createElement("button");
      btnQt.className = "act-btn";
      btnQt.type = "button";
      btnQt.title = "引用回复";
      btnQt.innerHTML = svgIcon("quote");
      btnQt.addEventListener("click", function (e) {
        e.stopPropagation();
        var snippet = body.innerText.split("\n").slice(0, 3).join("\n");
        input.value = "> " + snippet + "\n\n" + input.value;
        input.focus();
        syncSendState();
      });

      var btnSpk = document.createElement("button");
      btnSpk.className = "act-btn";
      btnSpk.type = "button";
      btnSpk.title = "朗读消息";
      btnSpk.innerHTML = svgIcon("speaker");
      btnSpk.addEventListener("click", function (e) {
        e.stopPropagation();
        speakText(body.innerText, btnSpk);
      });

      actBar.appendChild(btnQt);
      actBar.appendChild(btnSpk);
    }
    bubble.appendChild(actBar);

    var btns = null;
    if (markup && markup.length) {
      btns = renderButtons(markup, messageId);
      if (btns) bubble.appendChild(btns);
    }

    row.appendChild(bubble);
    log.appendChild(row);
    if (stick) scrollDown();

    var entry = { row: row, body: body, bubble: bubble, head: head, btns: btns, rawText: text, parseMode: parseMode, role: role };
    if (messageId != null) byMessageId[messageId] = entry;
    if (role === "ai" || role === "cmd" || role === "sys") lastStreamEntry = entry;
    return entry;
  }

  var IMG_EXT = { jpg: 1, jpeg: 1, png: 1, gif: 1, webp: 1, bmp: 1, avif: 1 };
  var VIDEO_EXT = { mp4: 1, webm: 1, mov: 1, m4v: 1, mkv: 1 };
  var AUDIO_EXT = { mp3: 1, wav: 1, ogg: 1, m4a: 1, flac: 1, aac: 1 };
  function mediaKindFromName(filename) {
    var m = /\.([a-zA-Z0-9]+)$/.exec(String(filename || ""));
    var ext = m ? m[1].toLowerCase() : "";
    if (IMG_EXT[ext]) return "photo";
    if (VIDEO_EXT[ext]) return "video";
    if (AUDIO_EXT[ext]) return "audio";
    return "file";
  }

  var lightbox = document.getElementById("lightbox");
  var lightboxImg = document.getElementById("lightbox-img");
  var lightboxFilename = document.getElementById("lightbox-filename");
  var lightboxDl = document.getElementById("lightbox-dl");
  var lightboxExt = document.getElementById("lightbox-ext");
  var currentRotate = 0;

  function openLightbox(url, name) {
    currentRotate = 0;
    lightboxImg.style.transform = "none";
    lightboxImg.src = url;
    lightboxFilename.textContent = name || "图片预览";
    lightboxDl.href = url;
    lightboxDl.download = name || "image.png";
    lightboxExt.href = url;
    lightbox.classList.add("open");
  }
  function closeLightbox() {
    lightbox.classList.remove("open");
    lightboxImg.src = "";
  }
  document.getElementById("lightbox-close").addEventListener("click", closeLightbox);
  document.getElementById("lightbox-rotate").addEventListener("click", function () {
    currentRotate = (currentRotate + 90) % 360;
    lightboxImg.style.transform = "rotate(" + currentRotate + "deg)";
  });
  lightbox.addEventListener("click", function (e) {
    if (e.target === lightbox || e.target.classList.contains("lightbox-img-wrap")) closeLightbox();
  });

  function sizeImagePlaceholder(container, width, height) {
    if (!(Number.isFinite(width) && width > 0 && Number.isFinite(height) && height > 0)) return;
    var scale=Math.min(1,440/width,420/height);
    container.style.setProperty('--media-photo-width',(width*scale)+'px');
    container.style.setProperty('--media-photo-ratio',width+' / '+height);
  }

  function updateMediaContent(container, item) {
    container._mediaItem=item;
    if(item.kind==='photo')sizeImagePlaceholder(container,item.width,item.height);
    container.querySelectorAll('a.media-dl,a.fc-dl').forEach(function(link){
      link.href=item.download_url;link.download=item.filename||'file';
    });
    var versions=container.querySelector('.media-versions');
    if(item.versions_id&&!versions){
      versions=document.createElement('button');versions.type='button';versions.className='media-versions';versions.textContent='版本';
      versions.addEventListener('click',function(e){
        e.stopPropagation();
        if(window.XGentWorkbench?.attachmentVersions)window.XGentWorkbench.attachmentVersions(container._mediaItem);
        else toast('附件详情模块未加载，请刷新后重试。',true);
      });
      container.appendChild(versions);
    }else if(!item.versions_id&&versions)versions.remove();
  }

  function canReuseMediaContent(container, item) {
    var previous=container._mediaItem;
    if(!previous||previous.kind!==item.kind||previous.error!==item.error)return false;
    if(previous.sha256&&item.sha256&&previous.sha256!==item.sha256)return false;
    // Accepted uploads retain their decoded local preview. Only metadata and
    // download links change; the same image element and blob URL stay visible.
    if(previous.local_preview&&previous.filename===item.filename&&item.download_url)return true;
    if(previous.path&&item.path)return previous.path===item.path;
    return previous.download_url===item.download_url&&previous.filename===item.filename;
  }

  function imageSourceUrl(item) {
    var url=item.download_url;
    if(url&&!/^(blob:|data:)/.test(url)&&/^[0-9a-f]{64}$/.test(item.sha256||'')){
      return url+(url.includes('?')?'&':'?')+'image_version='+item.sha256;
    }
    return url;
  }

  function makeMediaContent(item) {
    var kind = item.kind, filename = item.filename, downloadUrl = item.download_url;
    var container = document.createElement("div");
    container.className = "media-item";
    if (downloadUrl || kind === 'photo') {

      var media = null;
      if (kind === "photo") {
        container.classList.add('media-photo');
        container.dataset.imageState='loading';
        sizeImagePlaceholder(container,item.width,item.height);
        var imageFrame=document.createElement('div');imageFrame.className='media-image-frame';
        imageFrame.setAttribute('aria-busy','true');
        var state=document.createElement('span');state.className='media-load-state';state.textContent='图片加载中…';
        var retry=document.createElement('button');retry.type='button';retry.className='media-image-retry';retry.textContent='重新加载';retry.hidden=true;
        media = document.createElement("img");
        media.loading = "lazy";
        media.decoding = 'async';
        media.className = "media-img";
        media.alt = filename || "图片";
        media.addEventListener("click", function () { openLightbox(media.src, container._mediaItem.filename); });
        media.addEventListener('load',function(){
          var source=media.src;
          sizeImagePlaceholder(container,media.naturalWidth,media.naturalHeight);
          var decoded=media.decode?media.decode().catch(function(){}):Promise.resolve();
          decoded.then(function(){
            if(media.src!==source||!media.naturalWidth)return;
            container.dataset.imageState='ready';imageFrame.setAttribute('aria-busy','false');
          });
        });
        media.addEventListener("error", function () {
          container.dataset.imageState='error';imageFrame.setAttribute('aria-busy','false');
          state.textContent='图片加载失败';retry.hidden=!container._mediaItem.download_url;
        });
        retry.addEventListener('click',function(){
          container.dataset.imageState='loading';imageFrame.setAttribute('aria-busy','true');
          state.textContent='图片加载中…';retry.hidden=true;
          var url=imageSourceUrl(container._mediaItem);
          media.src=url+(url.startsWith('blob:')?'':(url.includes('?')?'&':'?')+'image_retry='+Date.now());
        });
        imageFrame.append(media,state,retry);container.appendChild(imageFrame);
        if(downloadUrl)media.src=imageSourceUrl(item);
        else{container.dataset.imageState='error';state.textContent=item.error||'图片暂不可下载';imageFrame.setAttribute('aria-busy','false');}
      } else if (kind === "video") {
        media = document.createElement("video");
        media.src = downloadUrl;
        media.controls = true;
        media.preload = "metadata";
        media.className = "media-video";
      } else if (kind === "audio") {
        media = document.createElement("audio");
        media.src = downloadUrl;
        media.controls = true;
        media.preload = "metadata";
        media.className = "media-audio";
      }

      if (media && kind !== 'photo') {
        container.appendChild(media);
      } else if (!media) {
        var card = document.createElement("div");
        card.className = "file-card";
        var fcIcon = document.createElement("div");
        fcIcon.className = "fc-icon";
        fcIcon.innerHTML = svgIcon("attach");
        var fcBody = document.createElement("div");
        fcBody.className = "fc-body";
        var fcName = document.createElement("div");
        fcName.className = "fc-name";
        fcName.textContent = filename || "文件";
        fcBody.appendChild(fcName);
        var fcBtn = document.createElement("a");
        fcBtn.className = "fc-dl";
        fcBtn.href = downloadUrl;
        fcBtn.download = filename || "file";
        fcBtn.target = "_blank";
        fcBtn.rel = "noopener noreferrer";
        fcBtn.innerHTML = svgIcon("down") + "<span>下载</span>";
        card.appendChild(fcIcon);
        card.appendChild(fcBody);
        card.appendChild(fcBtn);
        card.addEventListener("click", function (e) {
          if (e.target.closest(".fc-dl")) return;
          fcBtn.click();
        });
        container.appendChild(card);
      }

      var dlBtn = document.createElement("a");
      dlBtn.className = "media-dl";
      dlBtn.href = downloadUrl;
      dlBtn.download = filename || "file";
      dlBtn.title = "下载文件";
      dlBtn.setAttribute("aria-label", "下载 " + (filename || "文件"));
      dlBtn.innerHTML = svgIcon("down");
      if (media && downloadUrl) container.appendChild(dlBtn);
    } else {
      container.classList.add("media-error");
      container.textContent = (filename || (kind === "photo" ? "图片" : "文件")) +
        "\n" + (item.error || "文件暂不可下载");
    }
    updateMediaContent(container,item);
    return container;
  }

  function setInlineMedia(entry, items) {
    var stick = isNearBottom();
    if (!items.length) {
      if (entry.mediaContainer) entry.mediaContainer.remove();
      if (entry.mediaPaths) entry.mediaPaths.remove();
      entry.mediaContainer = entry.mediaPaths = null;
      entry.bubble.classList.remove("has-media");
      return;
    }
    if (!entry.mediaContainer) {
      entry.mediaContainer = document.createElement("div");
      entry.mediaContainer.className = "message-media";
      entry.bubble.insertBefore(entry.mediaContainer, entry.body);
    }
    var previousItems=Array.from(entry.mediaContainer.children),retainedItems=new Set();
    var paths = [];
    var displayedText = String(entry.rawText || "").replace(/\\/g, "/");
    items.forEach(function (item,index) {
      var content=previousItems.find(node=>!retainedItems.has(node)&&canReuseMediaContent(node,item));
      if(content)updateMediaContent(content,item);
      else content=makeMediaContent(item);
      retainedItems.add(content);
      if(entry.mediaContainer.children[index]!==content)entry.mediaContainer.insertBefore(content,entry.mediaContainer.children[index]||null);
      if (item.path && displayedText.indexOf(item.path.replace(/\\/g, "/")) < 0) paths.push(item.path);
    });
    previousItems.forEach(function(node){if(!retainedItems.has(node))node.remove();});
    entry.bubble.classList.toggle("has-media", items.length > 0);
    if (entry.mediaPaths) entry.mediaPaths.remove();
    entry.mediaPaths = null;
    if (paths.length) {
      entry.mediaPaths = document.createElement("div");
      entry.mediaPaths.className = "media-paths";
      if(entry.role==='user'){
        entry.mediaPaths.classList.add('media-paths-compact');
        paths.forEach(function(path){
          var button=document.createElement('button');button.type='button';button.className='media-path-copy';
          button.textContent='已保存 · '+path;button.title=path+'（点击复制）';
          button.setAttribute('aria-label','复制服务器路径 '+path);
          button.addEventListener('click',()=>copyText(path).then(()=>toast('已复制服务器路径')).catch(()=>toast('复制失败',true)));
          entry.mediaPaths.appendChild(button);
        });
      }else entry.mediaPaths.textContent = paths.join("\n");
      entry.body.insertAdjacentElement("afterend", entry.mediaPaths);
    }
    if (stick) scrollDown();
  }

  function makeMediaBubble(kind, filename, caption, downloadUrl, messageId, side, timestamp, error, descriptor) {
    var entry = makeBubble(side || "ai", caption || "", messageId, null, null, null, timestamp);
    entry.bubble.classList.add("media-bubble");
    setInlineMedia(entry, [{...descriptor, kind: kind, filename: filename, download_url: downloadUrl, error: error }]);
    return entry;
  }

  function renderButtons(rows, messageId, ui) {
    var container = document.createElement("div");
    container.className = "inline-keyboard";
    rows.forEach(function (row) {
      var rowEl = document.createElement("div");
      rowEl.className = "ik-row";
      row.forEach(function (btn) {
        if (btn.url && /^(https?:|tg:)/i.test(btn.url)) {
          var link = document.createElement("a");
          link.className = "ik-btn";
          link.textContent = btn.text;
          link.href = btn.url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          rowEl.appendChild(link);
          return;
        }
        if (!btn.callback_data && !btn.unavailable) return;
        var b = document.createElement("button");
        b.className = "ik-btn";
        b.textContent = btn.text;
        b.disabled = !!btn.unavailable;
        if (btn.unavailable) b.title = "菜单已失效，请重新打开";
        b.addEventListener("click", function () {
          if (b.disabled) return;
          b.disabled = true; b.classList.add("loading");
          triggerHaptic();
          var payload = ui ? {ui_message_id: ui.id, revision: ui.revision, button_id: btn.button_id} :
            {callback_data: btn.callback_data, message_id: messageId};
          var requestId=crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+Math.random().toString(36).slice(2);
          payload.request_id=requestId;
          cardScrollIntent={id:requestId,cid:selectedConversationId,messageId:ui?'ui:'+ui.id:messageId};
          cardRequests.set(requestId,{button:b,timer:setTimeout(()=>{finishCardRequest(requestId);if(cardScrollIntent?.id===requestId)cardScrollIntent=null;},180000)});
          api("/api/callback", { method: "POST", body: JSON.stringify(payload) })
            .catch(function (err) { finishCardRequest(requestId);if(cardScrollIntent?.id===requestId)cardScrollIntent=null;toast(err.message); });
        });
        rowEl.appendChild(b);
      });
      if (rowEl.childNodes.length) container.appendChild(rowEl);
    });
    return container.childNodes.length ? container : null;
  }

  function setButtons(entry, markup, messageId) {
    if (entry.btns && entry.btns.parentNode) entry.btns.parentNode.removeChild(entry.btns);
    entry.btns = null;
    if (markup && markup.length) {
      entry.btns = renderButtons(markup, messageId, entry.ui);
      if (entry.btns && entry.bubble) entry.bubble.appendChild(entry.btns);
    }
  }

  var conversationBusy = false;
  function setBusy(busy) {
    conversationBusy = !!busy;
    updateSendAvailability();
    btnStop.classList.toggle("hidden", !busy);
    botAvatar.classList.toggle("pulsing", busy);
    setConnState(currentConnState);
    var running=window.XGentConversations?.running;
    if (busy && (!running || running.conversation_id===selectedConversationId)) showTyping(); else hideTyping();
  }
  function showTyping() { typing.classList.remove("hidden"); if (isNearBottom()) scrollDown(); }
  function hideTyping() { typing.classList.add("hidden"); }

  function removeEntry(entry) {
    if (!entry) return;
    Object.keys(byMessageId).forEach(function (id) {
      if (byMessageId[id] === entry) delete byMessageId[id];
    });
    (entry.mediaEntries || []).forEach(removeEntry);
    (entry.objectUrls || []).forEach(function (url) { URL.revokeObjectURL(url); });
    if (entry.row && entry.row.parentNode) {
      entry.row.parentNode.removeChild(entry.row);
      msgCount = Math.max(0, msgCount - 1);
    }
    if (lastStreamEntry === entry) lastStreamEntry = null;
    updateEmptyState();
  }

  function putTextFrame(role, frame) {
    var id = frame.ui_message_id ? "ui:" + frame.ui_message_id :
      (frame.media_group_id || (frame.record_id != null ? "history:" + frame.record_id :
        (frame.web_display?.reply_message_id ?? frame.message_id)));
    var entry = id != null ? byMessageId[id] : null;
    // An already saved reply must not regress to a delayed transport fragment.
    if(entry?.durable && frame.record_id==null && !frame.ui_message_id)return entry;
    (frame.replace_message_ids || []).forEach(function (previousId) {
      if (byMessageId[previousId] !== entry) removeEntry(byMessageId[previousId]);
    });
    if (!entry && frame.type === "edit" && id == null) entry = lastStreamEntry;
    var newRole = classifyMessageText(role, frame.text, frame.msg_type);
    var displayKey=JSON.stringify(frame.web_display?.segments||null);
    if (entry && entry.rawText !== undefined && entry.role === newRole) {
      if(entry.displayKey!==displayKey || (!frame.web_display &&
          (entry.rawText!==frame.text || entry.parseMode!==frame.parse_mode))){
        repaintBubble(entry,renderBubbleBody(newRole, frame.text, frame.parse_mode, frame.web_display));
      }
    } else {
      var next = entry && entry.row.nextSibling;
      removeEntry(entry);
      entry = makeBubble(role, frame.text, id, frame.parse_mode, null, frame.msg_type, frame.timestamp || frame.ts, frame.web_display);
      if (next && next.parentNode === log) log.insertBefore(entry.row, next);
    }
    entry.rawText=frame.text;entry.parseMode=frame.parse_mode;entry.displayKey=displayKey;
    if(frame.timestamp!=null){
      var ts=entry.row.querySelector('.ts');
      if(ts){ts.textContent=nowTime(frame.timestamp);ts.title=new Date(Number(frame.timestamp)*1000).toLocaleString();}
    }
    bindUiMessage(entry, frame);
    if (frame.reply_markup !== undefined) {
      setButtons(entry, frame.reply_markup, frame.message_id != null ? frame.message_id : (frame.record_id || 0));
    }
    if (frame.media) appendHistoryMedia(entry, frame, role);
    if (frame.message_id != null && !frame.ui_message_id) byMessageId[frame.message_id] = entry;
    return entry;
  }

  function putHistoryFrame(message, epoch) {
    var role=message.role==='user'?'user':(message.role==='assistant'?'ai':'sys');
    var id=message.ui_message_id?'ui:'+message.ui_message_id:
      (message.media_group_id||'history:'+(message.record_id??message.id));
    var aliases=message.replace_message_ids||[];
    if(message.upload_request_key)aliases=aliases.concat('upload:'+message.upload_request_key);
    if(message.web_live&&message.web_live.epoch===epoch)aliases=aliases.concat(message.web_live.message_ids||[]);
    var existing=byMessageId[id];
    // Adopt the existing live bubble rather than throw away expanded blocks and
    // selections merely to replace a transport ID with the durable record ID.
    if(!existing){
      existing=aliases.map(mid=>byMessageId[mid]).find(Boolean);
      if(existing)byMessageId[id]=existing;
    }
    var frame={...message,type:'message',text:message.content??message.text,
      record_id:message.record_id??message.id,replace_message_ids:aliases};
    var entry=putTextFrame(role,frame);
    if(message.file_message&&entry.uploadProgress){entry.uploadProgress.remove();entry.uploadProgress=null;}
    entry.durable=!message.ui_message_id;
    aliases.forEach(function(mid){byMessageId[mid]=entry;});
    entry.row.dataset.historyCursor=message.history_cursor||entry.row.dataset.historyCursor||'';
    if(frame.record_id!=null&&!message.ui_message_id)entry.row.dataset.recordId=String(frame.record_id);
    return entry;
  }

  function bindUiMessage(entry, frame) {
    if (!frame.ui_message_id) return;
    entry.ui = {id: frame.ui_message_id, revision: frame.revision, generation: frame.ui_generation};
    entry.row.dataset.uiMessageId = frame.ui_message_id;
    entry.row.dataset.uiRevision = String(frame.revision);
  }

  function acceptUiFrame(frame) {
    if (!frame.ui_message_id) return true;
    if (!Number.isSafeInteger(frame.ui_generation) || !Number.isSafeInteger(frame.revision)) return false;
    if (uiGeneration !== null && frame.ui_generation < uiGeneration) return false;
    if (uiGeneration !== null && frame.ui_generation > uiGeneration) {
      // UI frames are persisted before delivery. Fetch their new generation;
      // never put an unsequenced raw frame into the transport replay queue.
      resync(true).catch(function(){});
      return false;
    }
    uiGeneration = frame.ui_generation;
    var id = frame.ui_message_id;
    if (uiDeleted[id] || frame.revision <= (uiRevisions[id] || 0)) return false;
    uiRevisions[id] = frame.revision;
    if (frame.type === "delete") uiDeleted[id] = true;
    return true;
  }

  function refreshMenus() {
    var generation = historyGen;
    return api("/api/workbench/history?limit=50").then(function (data) {
      if (!historyReady || generation !== historyGen) return;
      if (Number.isSafeInteger(data.ui_generation) && data.ui_generation !== uiGeneration) {
        if (uiGeneration === null || data.ui_generation > uiGeneration) return resync();
        return;
      }
      (data.ui_tombstones || []).forEach(function (item) {
        handleFrame(Object.assign({}, item, {type: "delete"}));
      });
      (data.messages || []).forEach(function (item) {
        if (item.ui_message_id) handleFrame(Object.assign({}, item, {type: "message", text: item.content}));
      });
    }).catch(function (err) { toast("菜单同步失败：" + err.message); });
  }

  function handleFrame(frame) {
    if(!['message','edit','edit_markup','delete','user_message','document','photo','notice','record_snapshot','attachment_changed'].includes(frame.type)){
      handleFrameContent(frame);return;
    }
    var anchor=scrollAnchor(),follow=isNearBottom()&&!viewingPast;
    renderingHistory++;
    try{handleFrameContent(frame);}finally{
      renderingHistory--;
      if(follow)scrollDown();else restoreScrollAnchor(anchor);
    }
  }

  function handleFrameContent(frame) {
    if(frame.type==='attachment_changed'){
      window.dispatchEvent(new CustomEvent('xgent-attachment-changed',{detail:frame}));
      if(frame.conversation_id===selectedConversationId&&frame.generation===uiGeneration){
        var existing=byMessageId['history:'+frame.record_id];
        if(existing&&frame.message)putHistoryFrame(frame.message,streamEpoch);
      }
      return;
    }
    if(frame.type==='queue_state'){refreshQueue();return;}
    if(frame.queue_id)refreshQueue();
    if(frame.type==='settings_state'){window.XGentAppearance?.accept(frame);return;}
    if(frame.type==='knowledge_state'){window.dispatchEvent(new CustomEvent('xgent-workbench-change',{detail:{pages:['models','settings']}}));return;}
    if(frame.type==='conversation_deleted'){window.dispatchEvent(new CustomEvent('xgent-conversation-deleted',{detail:frame}));window.XGentConversations?.refresh();return;}
    if(frame.interaction_id&&['callback_done','turn_error'].includes(frame.type))finishCardRequest(frame.interaction_id);
    if(frame.type === 'conversation_state') {
      window.XGentConversations?.accept(frame);
      setBusy(!!frame.running);
      return;
    }
    if(frame.type === 'turn_rejected') {
      var rejectedCid=frame.conversation_id || selectedConversationId;
      if(frame.rejected_text) {
        conversationDrafts[rejectedCid]=frame.rejected_text;
        try{sessionStorage.setItem(draftKey(rejectedCid),frame.rejected_text);}catch(e){}
        if(rejectedCid===selectedConversationId&&!input.value)input.value=frame.rejected_text;
      }
      if(uploadQueue&&uploadQueue.conversation_id===rejectedCid&&uploadQueue.finish){uploadQueue.rejected=true;uploadQueue.error=frame.text;uploadQueue.awaitingTurn=false;uploadQueue.finish(frame.text);}
      toast(frame.text||'请求未提交',true);
      setBusy(!!window.XGentConversations?.running);
      return;
    }
    if(frame.conversation_id && selectedConversationId && frame.conversation_id !== selectedConversationId) {
      if(uploadQueue?.conversation_id===frame.conversation_id && ['turn_end','turn_error'].includes(frame.type) && uploadQueue.finish){
        uploadQueue.awaitingTurn=false;uploadQueue.finish(frame.type==='turn_error'?frame.text:null);
      }
      return;
    }
    if(frame.generation!=null && uiGeneration!=null && frame.generation<uiGeneration)return;
    var running=window.XGentConversations?.running;
    var completion=['turn_end','turn_error','generation_end'].includes(frame.type)||(frame.type==='compression_state'&&!frame.busy);
    if(completion&&running&&(!frame.run_id||frame.run_id!==running.run_id)){
      if(!frame.run_id&&frame.type==='turn_error'&&frame.text)toast(frame.text,true);
      return;
    }
    if(frame.type==='turn_end'||frame.type==='turn_error')window.dispatchEvent(new Event('xgent-turn-finished'));
    if(viewingPast && ['message','edit','edit_markup','user_message','document','photo','notice','record_snapshot'].includes(frame.type) &&
       (!frame.ui_generation || frame.ui_generation===uiGeneration) && !displayedFrameEntry(frame)) {
      var newId=frame.web_display?.reply_message_id??frame.ui_message_id??frame.record_id??frame.message_id;
      if(newId==null||!unseenHistoryFrames.has(newId)){newFrames++;unseenHistoryFrames.add(newId);}
      newerAvailable=true;updateHistoryState();return;
    }
    if (isNearBottom()) requestAnimationFrame(function(){boundMessages(false);});
    if (!acceptUiFrame(frame)) return;
    if(frame.type==='record_snapshot'){
      if(Number.isSafeInteger(frame.ui_generation)&&uiGeneration!==null&&frame.ui_generation!==uiGeneration){
        if(frame.ui_generation>uiGeneration)resync(true).catch(function(){});
        return;
      }
      var anchor=Array.from(log.children).find(row=>row.getBoundingClientRect().bottom>log.getBoundingClientRect().top);
      var top=anchor?.getBoundingClientRect().top,stick=isNearBottom();
      putHistoryFrame(frame,streamEpoch);
      if(stick)scrollDown();else if(anchor?.isConnected)log.scrollTop+=anchor.getBoundingClientRect().top-top;
    } else if (frame.type === "message") {
      putTextFrame("ai", frame);
      hideTyping();
    } else if (frame.type === "edit") {
      putTextFrame("ai", frame);
      hideTyping();
    } else if (frame.type === "edit_markup") {
      if (frame.ui_message_id) {
        putTextFrame("ai", frame);
      } else {
        var em = byMessageId[frame.message_id];
        if (em) setButtons(em, frame.reply_markup, frame.message_id);
      }
    } else if (frame.type === "delete") {
      if(frame.web_display?.reply_message_id!=null&&frame.message_id!==frame.web_display.reply_message_id){
        delete byMessageId[frame.message_id];return;
      }
      var deletedId = frame.ui_message_id ? "ui:" + frame.ui_message_id : frame.message_id;
      var d = byMessageId[deletedId];
      if(d?.durable&&!frame.ui_message_id)return;
      removeEntry(d);
      delete byMessageId[deletedId];
    } else if (frame.type === "user_message") {
      putTextFrame("user", frame);
      hideTyping();
    } else if (frame.type === "notice") {
      putTextFrame("sys", frame);
    } else if (frame.type === "document" || frame.type === "photo") {
      hideTyping();
      var oldMedia = byMessageId[frame.message_id];
      var nextMedia = oldMedia && oldMedia.row.nextSibling;
      removeEntry(oldMedia);
      var mediaKind = frame.type === "photo" ? "photo" : mediaKindFromName(frame.filename);
      var mediaEntry = makeMediaBubble(mediaKind, frame.filename, frame.caption, frame.download_url, frame.message_id, "ai", frame.timestamp || frame.ts);
      if (nextMedia && nextMedia.parentNode === log) log.insertBefore(mediaEntry.row, nextMedia);
    } else if (frame.type === "chat_action") {
      if (frame.action === "typing") showTyping(); else hideTyping();
    } else if (frame.type === "callback_answer") {
      toast(frame.text, frame.show_alert);
    } else if (frame.type === "callback_done") {
      const refreshed=frame.resync?refreshMenus():Promise.resolve();
      Promise.resolve(refreshed).then(()=>finishCardScroll(frame)).catch(()=>finishCardRequest(frame.interaction_id));
    } else if (frame.type === "generation_end") {
      setBusy(false);hideTyping();
    } else if (frame.type === "turn_end" || frame.type === "turn_error") {
      setBusy(false);
      var error = frame.type === "turn_error" ? (frame.text || "处理失败") : null;
      var queuedTurn = uploadQueue;
      if (queuedTurn && queuedTurn.finish) {
        queuedTurn.awaitingTurn = false;
        if (error) queuedTurn.error = error;
        queuedTurn.finish(error);
      }
      var completed = Promise.resolve();
      // A completed turn is already represented by send/edit/delete frames.
      // Re-reading DB here used to erase the live placeholder and rebuild every bubble.
      reconcileAfterTurn = false;
      if (error && !queuedTurn) completed.then(function () {
        var text = error.indexOf('⚠️ ') === 0 ? error : '⚠️ ' + error;
        if (frame.record_id != null) putTextFrame('sys', {type:'message', record_id:frame.record_id,
          text:text, msg_type:'runtime_error', parse_mode:null});
        else makeBubble('sys', text);
      });
    } else if (frame.type === "compression_state") {
      setBusy(!!frame.busy);
      if (!frame.busy) {
        reconcileAfterTurn = false;
        // Restore failures and retry controls are durable after the archive switch.
        if (frame.committed) resync(true).catch(function(){});
      }
    } else if (frame.type === "history_reset" || frame.type === "context_reset") {
      cancelUploads("记忆已清空");
      pendingFrames = [];
      // An earlier baseline may be resolving and flushing this very event.
      // Invalidation must replace it, not coalesce into that stale request.
      resync(true).catch(function(){});
    }
  }

  var currentConnState = "";
  var historyReady = false;
  var loggedOut = false;
  var pendingFrames = [];
  var historyGen = 0;
  var reconcileAfterTurn = false;

  var historyRequest = null;
  var snapshotWatermark = null;
  function applyFrame(frame) { handleFrame(frame); }

  function flushPending() {
    historyReady = true;
    var queued = pendingFrames; pendingFrames = [];
    queued.forEach(function(event){
      try { receiveEvent(event.epoch,event.id,event.frame); }
      catch(e){console.error('消息渲染失败',e);}
    });
    updateSendAvailability();
    scheduleHistoryPrefetch();
    refreshQueue();
  }

  function resync(force) {
    if(historyRequest&&!force)return historyRequest;
    historyReady=false;btnSend.disabled=true;
    var requestGeneration=historyGen+1;
    var request=loadHistory().then(function(){if(requestGeneration===historyGen)flushPending();}).catch(function(err){
      if(requestGeneration!==historyGen)return;
      toast('历史加载失败：'+err.message,true);
      setConnState('disconnected');
      // Do not call flushPending after failed history: the baseline is unknown.
      throw err;
    }).finally(function(){if(historyRequest===request)historyRequest=null;});
    historyRequest=request;
    return request;
  }

  function setConnState(state) {
    currentConnState = state || "";
    var cls = "conn-dot" + (state ? " " + state : "");
    if (state === "connecting") statusText.innerHTML = '<span class="' + cls + '"></span> 连接中…';
    else if (state === "disconnected") statusText.innerHTML = '<span class="' + cls + '"></span> 已断开，重连中…';
    else if (conversationBusy) statusText.innerHTML = '<span class="conn-dot"></span> 思考与生成中…';
    else statusText.innerHTML = '<span class="' + cls + '"></span> 在线';
    statusText.classList.toggle("busy", conversationBusy && !state);
    updateSendAvailability();
  }

  function stopLiveTransport() {
    streamGeneration++;
    if (evtSource) { evtSource.close(); evtSource = null; }
    if (pollController) { pollController.abort(); pollController = null; }
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (streamWatchdog) { clearTimeout(streamWatchdog); streamWatchdog = null; }
  }

  function receiveEvent(epoch,id,frame){
    if(!historyReady){
      pendingFrames.push({epoch:epoch,id:id,frame:frame});
      if(pendingFrames.length>2000){pendingFrames=[];startPolling();}
      return;
    }
    if(epoch!==streamEpoch){
      if(snapshotWatermark && epoch===snapshotWatermark.epoch)return;
      pendingFrames.push({epoch:epoch,id:id,frame:frame});
      resync().catch(function(){});return;
    }
    if(id<=streamCursor)return;
    if(id!==streamCursor+1){startPolling();return;}
    streamCursor=id;applyFrame(frame);
  }

  function startPolling() {
    if(loggedOut)return;
    stopLiveTransport();
    preferPolling = true;
    var generation = streamGeneration;
    setConnState("connecting");
    async function poll() {
      if (generation !== streamGeneration) return;
      var controller = new AbortController();
      pollController = controller;
      var timeout = setTimeout(function () { controller.abort(); }, 30000);
      try {
        if(!historyReady || streamCursor===null)await resync();
        if(generation!==streamGeneration)return;
        // A long poll waits for future changes, not initial readiness. Enable the
        // composer as soon as the authenticated snapshot is available.
        setConnState();
        var query = streamCursor === null ? "" : "?after=" + streamCursor + "&epoch=" + encodeURIComponent(streamEpoch) + "&wait=20";
        var data = await api("/api/events" + query, { cache: "no-store", signal: controller.signal });
        if (generation !== streamGeneration) return;
        if (!data.epoch || !Number.isSafeInteger(data.cursor) || !Array.isArray(data.events)) {
          throw new Error("实时更新响应无效");
        }
        var reset = !!data.reset || (streamEpoch !== null && data.epoch !== streamEpoch);
        var first = streamEpoch === null;
        if (first || reset) {
          pendingFrames = [];
          setConnState("connecting");
          if (reset) cancelUploads("连接已重新同步");
        }
        if (first || reset) {
          await resync();
          if (generation !== streamGeneration) return;
          if (reset) toast("连接已恢复，已重新同步历史记录");
        }
        if (!first && !reset) {
          data.events.forEach(function (event) {
            if (generation === streamGeneration) receiveEvent(event.epoch, event.id, event.frame);
          });
          if (generation !== streamGeneration) return;
          streamCursor = Math.max(streamCursor, data.cursor);
        }
        setConnState(data.more ? "connecting" : "");
        reconnectDelay = 1000;
        reconnectTimer = setTimeout(poll, 0); // Server long-poll wakes immediately when an event arrives.
      } catch (err) {
        if (generation !== streamGeneration) return;
        reconcileAfterTurn = true;
        cancelUploads("连接中断");
        setConnState("disconnected");
        reconnectTimer = setTimeout(poll, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.5, 30000);
      } finally {
        clearTimeout(timeout);
        if (pollController === controller) pollController = null;
      }
    }
    poll();
  }

  function connectStream() {
    if(loggedOut)return;
    liveStarted = true;
    if (preferPolling || streamEpoch !== null || typeof EventSource === "undefined") { startPolling(); return; }
    stopLiveTransport();
    setConnState("connecting");
    var generation = streamGeneration;
    var source = evtSource = new EventSource("/api/stream");
    function watch(timeout) {
      if (streamWatchdog) clearTimeout(streamWatchdog);
      streamWatchdog = setTimeout(function () {
        if (generation === streamGeneration) startPolling();
      }, timeout);
    }
    // HTTP headers alone do not prove that a proxy forwards event bodies.
    watch(6000);
    source.addEventListener("ready", function (e) {
      if (generation !== streamGeneration) return;
      try {
        var data = JSON.parse(e.data);
        if (!data.epoch || !Number.isSafeInteger(data.cursor) || data.cursor < 0) {
          throw new Error("实时连接响应无效");
        }
        setConnState();reconnectDelay=1000;
        const syncing=historyRequest || (!historyReady?resync():Promise.resolve());
        syncing.then(function(){
          if(generation!==streamGeneration)return;
          if(streamEpoch!==data.epoch || streamCursor<data.cursor)startPolling();
        }).catch(function(){if(generation===streamGeneration)startPolling();});
        watch(25000);
      } catch (err) { startPolling(); }
    });
    source.addEventListener("ping", function () {
      if (generation === streamGeneration) watch(25000);
    });
    source.onmessage = function (e) {
      if (generation !== streamGeneration) return;
      try {
        var id = /^([a-f0-9]+):(\d+)$/.exec(e.lastEventId);
        if (!id) { startPolling(); return; }
        receiveEvent(id[1], Number(id[2]), JSON.parse(e.data));
        if (generation === streamGeneration) watch(25000);
      } catch (err) { startPolling(); }
    };
    source.onerror = function () {
      if (generation !== streamGeneration) return;
      if (navigator.onLine === false) disconnectStream(); else startPolling();
    };
  }

  function disconnectStream() {
    stopLiveTransport();
    reconcileAfterTurn = true;
    cancelUploads("连接中断");
    setConnState("disconnected");
    if (navigator.onLine !== false) {
      reconnectTimer = setTimeout(connectStream, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 1.5, 30000);
    }
  }

  window.addEventListener("offline", function () { if (liveStarted) disconnectStream(); });
  window.addEventListener("online", function () { if (liveStarted) connectStream(); });

  var historyCursor = null;
  var historyLoading = false;
  var historyPageRequest=null, historyLoadController=null, historyPageVersion=0;
  var historyError=null, historyPrefetchScheduled=false;
  var unseenHistoryFrames=new Set();
  var historyAnchor = null;
  var viewingPast = false;
  var newerAvailable = false;
  var newFrames = 0;
  function historyUrl() {
    return "/api/workbench/history?limit=50" + (historyAnchor ? "&anchor=" + encodeURIComponent(historyAnchor) : "");
  }
  function boundMessages(prepend) {
    var rows = Array.from(log.children);
    var removedFront=false;
    while (rows.length > 200) {
      var row = prepend ? rows.pop() : rows.shift();
      var selection=window.getSelection();
      var box=row.getBoundingClientRect(),viewport=log.getBoundingClientRect();
      if((box.bottom>viewport.top&&box.top<viewport.bottom) || row.contains(document.activeElement) || row.querySelector("blockquote.expanded") ||
         Array.from(row.querySelectorAll("video,audio")).some(function(media){return !media.paused;}) ||
         (selection&&!selection.isCollapsed&&(row.contains(selection.anchorNode)||row.contains(selection.focusNode)))) continue;
      var entry=null;
      Object.keys(byMessageId).forEach(function(key){if(byMessageId[key].row===row){entry=byMessageId[key];delete byMessageId[key];}});
      if(entry)removeEntry(entry);else row.remove();
      if(prepend){newerAvailable=true;viewingPast=true;}else removedFront=true;
    }
    if(removedFront)historyCursor=edgeCursor(false)||historyCursor;
  }
  function displayedFrameEntry(frame){
    var keys=[frame.ui_message_id?'ui:'+frame.ui_message_id:null,frame.media_group_id,
      frame.record_id!=null?'history:'+frame.record_id:null,frame.web_display?.reply_message_id,frame.message_id,
      ...(frame.replace_message_ids||[]),...(frame.web_live?.epoch===streamEpoch?frame.web_live.message_ids||[]:[])];
    return keys.map(key=>key!=null?byMessageId[key]:null).find(Boolean);
  }
  function scrollAnchor(){
    var row=Array.from(log.children).find(row=>row.getBoundingClientRect().bottom>log.getBoundingClientRect().top);
    return row?{row:row,top:row.getBoundingClientRect().top}:null;
  }
  function restoreScrollAnchor(anchor){
    if(anchor?.row.isConnected)log.scrollTop+=anchor.row.getBoundingClientRect().top-anchor.top;
  }
  function cancelHistoryPage(){
    historyPageVersion++;
    historyPageRequest?.controller.abort();historyPageRequest=null;
    historyLoading=false;historyError=null;
  }
  function scheduleHistoryPrefetch(){
    if(historyPrefetchScheduled)return;
    historyPrefetchScheduled=true;
    requestAnimationFrame(function(){
      historyPrefetchScheduled=false;
      if(historyReady&&!loggedOut&&!historyLoading&&!historyError&&historyCursor&&
         log.clientHeight>0&&log.scrollTop<=Math.max(800,1.5*log.clientHeight))historyPage(false);
    });
  }
  function edgeCursor(last) {
    var rows=Array.from(log.querySelectorAll('[data-history-cursor]'));
    return (last?rows.at(-1):rows[0])?.dataset.historyCursor;
  }
  function updateHistoryState() {
    document.getElementById('wb-chat').classList.toggle('history-past',viewingPast);
    document.getElementById('wb-chat').classList.toggle('history-top',log.scrollTop<8&&!!historyCursor);
    document.getElementById('wb-chat').classList.toggle('history-error',!!historyError);
    document.getElementById('wb-earlier').disabled=historyLoading||!historyCursor;
    document.getElementById('wb-newer').disabled=historyLoading||!newerAvailable;
    document.getElementById('wb-earlier').textContent=historyError&&!historyError.forward?'重试加载历史':'加载更早消息';
    document.getElementById('wb-newer').textContent=historyError?.forward?'重试较新消息':'加载较新消息';
    document.getElementById('wb-history-state').textContent=historyError?'加载失败：'+historyError.message:
      historyLoading?'正在加载…':viewingPast?
      ('浏览历史'+(newFrames?' · 有 '+newFrames+' 条新动态':'')+' · 可向前/后翻页'):'';
  }
  function historyControls(data) {
    historyCursor=data.next_cursor||null;
    newerAvailable=!!historyAnchor;
    viewingPast=!!historyAnchor;
    newFrames=0;unseenHistoryFrames.clear();
    updateHistoryState();
  }
  async function historyPage(forward) {
    var cursor=forward?edgeCursor(true):historyCursor;
    if(!cursor||historyLoading||(forward&&!newerAvailable))return;
    var request={controller:new AbortController(),version:++historyPageVersion,generation:historyGen,cid:selectedConversationId};
    historyPageRequest=request;historyLoading=true;historyError=null;updateHistoryState();
    var current=()=>historyPageRequest===request&&request.version===historyPageVersion&&
      request.generation===historyGen&&request.cid===selectedConversationId;
    try{
      var data=await api('/api/workbench/history?limit=50&'+(forward?'after=':'before=')+encodeURIComponent(cursor),{signal:request.controller.signal});
      if(!current())return;
      if(data.conversation_id&&request.cid&&data.conversation_id!==request.cid){
        throw new Error('会话已变化，请重试加载历史');
      }
      if(data.reset||(Number.isSafeInteger(data.ui_generation)&&uiGeneration!==null&&data.ui_generation!==uiGeneration)){
        historyAnchor=null;viewingPast=false;await resync(true);return;
      }
      // Capture NOW, not before the network request: the reader may have moved
      // several screens while a slow page was in flight.
      var anchor=scrollAnchor(),first=log.firstChild;
      renderingHistory++;
      try{
        (data.messages||[]).forEach(function(m){
          var id=m.history_key||(m.ui_message_id?'ui:'+m.ui_message_id:'history:'+m.id);
          if(byMessageId[id])return;
          if(m.ui_message_id&&(uiDeleted[m.ui_message_id]||m.revision<(uiRevisions[m.ui_message_id]||0)))return;
          var entry=putHistoryFrame(m,streamEpoch);
          if(m.ui_message_id)uiRevisions[m.ui_message_id]=m.revision;
          if(!forward){log.insertBefore(entry.row,first);(entry.mediaEntries||[]).forEach(media=>log.insertBefore(media.row,first));}
        });
        // Restore before pruning so the visible rows are protected by the window.
        restoreScrollAnchor(anchor);boundMessages(!forward);
        if(forward){newerAvailable=!!data.next_cursor;historyCursor=edgeCursor(false)||historyCursor;}
        else historyCursor=(data.messages?.length&&data.next_cursor!==cursor)?data.next_cursor||null:null;
        updateHistoryState();restoreScrollAnchor(anchor);
      }finally{renderingHistory--;}
    }catch(err){if(current()&&err.name!=='AbortError')historyError={forward:forward,message:err.message};}
    finally{
      if(current()){historyPageRequest=null;historyLoading=false;updateHistoryState();scheduleHistoryPrefetch();}
    }
  }
  document.getElementById('wb-earlier').onclick=function(){historyPage(false);};
  document.getElementById('wb-newer').onclick=function(){historyPage(true);};
  document.getElementById('wb-latest').onclick=function(){if(location.hash.includes('?conversation=')){history.replaceState(null,'','#/chat');document.getElementById('source-navigation').hidden=true;}historyAnchor=null;viewingPast=false;newFrames=0;resync(true).then(scrollDown).catch(function(){});};
  function loadHistory() {
    cancelHistoryPage();historyLoadController?.abort();
    var controller=historyLoadController=new AbortController();
    historyGen++;
    var gen=historyGen, requestedCid=selectedConversationId;
    var wasNearBottom=isNearBottom();
    var top=log.scrollTop;
    return api(historyUrl(),{signal:controller.signal}).then(function(data){
      if(gen!==historyGen || requestedCid!==selectedConversationId)return;
      if(data.conversation_id && requestedCid && data.conversation_id!==requestedCid){
        throw new Error('会话已变化，请重新加载');
      }
      if(!selectedConversationId && data.conversation_id){
        selectedConversationId=data.conversation_id;
        if(!input.value){try{input.value=sessionStorage.getItem(draftKey())||'';}catch(e){}}
        syncSendState();
      }
      historyControls(data);
      if(typeof data.queue_enabled==='boolean')queueEnabled=data.queue_enabled;
      const sameGeneration=uiGeneration===null || data.ui_generation==null || uiGeneration===data.ui_generation;
      if(!sameGeneration){uiRevisions={};uiDeleted={};}
      uiGeneration=Number.isSafeInteger(data.ui_generation)?data.ui_generation:null;
      (data.ui_tombstones||[]).forEach(function(item){uiRevisions[item.ui_message_id]=item.revision;uiDeleted[item.ui_message_id]=true;});
      const retained=new Set();
      (data.messages||[]).forEach(function(m){
        if(m.ui_message_id&&uiDeleted[m.ui_message_id])return;
        const entry=putHistoryFrame(m,data.live?.epoch);
        if(m.ui_message_id)uiRevisions[m.ui_message_id]=m.revision;
        retained.add(entry);log.appendChild(entry.row);(entry.mediaEntries||[]).forEach(child=>log.appendChild(child.row));
      });
      const live=data.live;
      if(live&&Number.isSafeInteger(live.cursor)&&live.epoch){
        snapshotWatermark={epoch:streamEpoch,cursor:streamCursor};
        streamEpoch=live.epoch;streamCursor=live.cursor;
        (live.frames||[]).filter(frame=>!frame.conversation_id||frame.conversation_id===selectedConversationId).forEach(function(frame){
          const entry=putTextFrame('ai',frame);retained.add(entry);log.appendChild(entry.row);
        });
      }
      // A snapshot taken while HTTP is still receiving cannot contain this
      // user message yet. Keep its one optimistic bubble until acceptance.
      var uploading=uploadQueue?.conversation_id===requestedCid?uploadQueue.entry:null;
      if(uploading&&!uploading.durable){retained.add(uploading);log.appendChild(uploading.row);}
      // Remove obsolete DOM only after incoming state is ready; no blank interstitial.
      Array.from(new Set(Object.values(byMessageId))).forEach(function(entry){
        if(!retained.has(entry)){
          removeEntry(entry);Object.keys(byMessageId).forEach(key=>{if(byMessageId[key]===entry)delete byMessageId[key];});
        }
      });
      const retainedRows=new Set();retained.forEach(entry=>{retainedRows.add(entry.row);(entry.mediaEntries||[]).forEach(child=>retainedRows.add(child.row));});
      Array.from(log.children).forEach(row=>{if(!retainedRows.has(row))row.remove();});
      if(typeof data.busy==='boolean')setBusy(data.busy);
      if(data.busy && !(live?.frames||[]).length && (!data.running || data.running.conversation_id===selectedConversationId))showTyping();else hideTyping();
      updateEmptyState();boundMessages(false);
      if(wasNearBottom||historyAnchor)scrollDown();else log.scrollTop=top;
      updateHistoryState();
      if(historyAnchor){const entry=byMessageId[historyAnchor];if(entry){entry.row.classList.add('wb-focus');setTimeout(()=>entry.row.classList.remove('wb-focus'),2000);}}
    }).finally(function(){if(historyLoadController===controller)historyLoadController=null;});
  }

  function appendHistoryMedia(entry, message, role) {
    var previousMedia=entry.mediaEntries||[];
    entry.mediaEntries = [];
    if (role === 'user' || message.media_group_id || message.msg_type === "ai_reply" || message.msg_type === "media_reply") {
      previousMedia.forEach(removeEntry);
      setInlineMedia(entry, (message.media || []).map(item=>({...item,conversation_id:message.conversation_id||selectedConversationId})));
    } else {
      var after = entry.row;
      (message.media || []).forEach(function (item,index) {
        item={...item,conversation_id:message.conversation_id||selectedConversationId};
        var card = previousMedia[index];
        if(card)setInlineMedia(card,[item]);
        else card=makeMediaBubble(item.kind, item.filename, item.path, item.download_url,
          null, role === "user" ? "user" : "ai", message.timestamp, item.error,item);
        log.insertBefore(card.row, after.nextSibling);
        after = card.row;
        entry.mediaEntries.push(card);
      });
      previousMedia.slice(entry.mediaEntries.length).forEach(removeEntry);
    }
    if (entry.mediaWarning) entry.mediaWarning.remove();
    entry.mediaWarning = null;
    if (message.media_error) {
      var warning = document.createElement("div");
      warning.className = "media-error";
      warning.textContent = message.media_error;
      entry.body.appendChild(warning);
      entry.mediaWarning = warning;
    }
  }

  function syncSendState() {
    btnSend.classList.toggle("ready", input.value.trim().length > 0 || pendingFiles.length > 0);
    updateSendAvailability();
  }

  function updateSendAvailability(){
    var queueable=queueEnabled&&!pendingFiles.length&&!input.value.trim().startsWith('/');
    btnSend.disabled=!!textSubmitting||!!uploadQueue||!historyReady||!!currentConnState||(conversationBusy&&!queueable);
    btnSend.classList.toggle('hidden',conversationBusy&&!queueEnabled);
    btnSend.title=conversationBusy&&queueable?'加入待发队列 (Enter)':'发送消息 (Enter)';
  }

  function queueRequestId(){return globalThis.crypto?.randomUUID?.()||('q'+Date.now().toString(36)+Math.random().toString(36).slice(2));}
  function queueReason(reason){return {waiting:'等待安全接入点',stopped:'已停止 · 手动继续',failed:'执行未完成 · 手动继续',restart:'来源进程已退出 · 手动继续',ask:'等待表单提交',ask_cancelled:'表单已取消 · 手动继续',generation:'上下文已改变 · 请取回重发',archived:'对话已归档'}[reason]||'排队中';}
  function drawQueue(error){
    var panel=document.getElementById('message-queue');if(!panel)return;
    var items=queueState?.conversation_id===selectedConversationId?queueState.items:[];
    var recovering=Array.from(queueActions.values()).filter(p=>p.action==='edit'&&p.item?.conversation_id===selectedConversationId&&!items?.some(i=>i.id===p.item.id));
    var anchor=scrollAnchor();
    panel.replaceChildren();panel.hidden=!items?.length&&!error&&!queueState?.paused&&!recovering.length;
    if(panel.hidden)return;
    var head=document.createElement('div');head.className='queue-head';
    var title=document.createElement('strong');title.textContent='待发消息 · '+(items?.length||0)+' 条';head.appendChild(title);
    function button(label,fn){var b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=fn;return b;}
    if(queueState?.paused&&queueState.reason!=='ask')head.appendChild(button('继续发送',()=>changeQueue(null,'resume')));
    if(items?.length>3)head.appendChild(button(queueExpanded?'收起':'展开全部',()=>{queueExpanded=!queueExpanded;drawQueue();}));
    panel.appendChild(head);
    var list=document.createElement('div');list.className='queue-list';
    (queueExpanded?items:items?.slice(0,3)||[]).forEach(item=>{
      var row=document.createElement('div');row.className='queue-item';row.dataset.queueId=item.id;
      var preview=document.createElement('div');preview.className='queue-preview';
      var p=document.createElement('p');p.textContent=item.text;p.title=item.text;
      var reason=document.createElement('small');reason.textContent=queueReason(item.reason)+' · '+item.origin;
      preview.append(p,reason);row.appendChild(preview);
      var actions=document.createElement('div');actions.className='queue-actions';
      ['cancel','edit'].forEach(action=>{var b=button(action==='cancel'?'❌ 取消':'编辑',()=>changeQueue(item,action));b.disabled=!!queueActions.get(item.id)?.busy;actions.appendChild(b);});
      row.appendChild(actions);list.appendChild(row);
    });panel.appendChild(list);
    recovering.forEach(p=>{
      var row=document.createElement('div');row.className='queue-actions';
      var text=document.createElement('span');text.textContent=p.busy?'正在取回消息…':'取回结果待确认，原文仍安全保留。';row.appendChild(text);
      var retry=button('重试取回',()=>changeQueue(p.item,'edit'));retry.disabled=p.busy;row.appendChild(retry);panel.appendChild(row);
    });
    if(error){var note=document.createElement('p');note.className='queue-error';note.textContent=error;panel.append(note,button('刷新队列',()=>refreshQueue(true)));}
    restoreScrollAnchor(anchor);
  }

  async function refreshQueue(force){
    if(loggedOut||!selectedConversationId||!historyReady)return;
    if(queueController&&!force){queueRefreshPending=true;return;}
    queueRefreshPending=false;
    queueController?.abort();var controller=queueController=new AbortController(),version=++queueVersion,cid=selectedConversationId;
    try{
      var response=await fetch('/api/queue?conversation_id='+encodeURIComponent(cid),{credentials:'same-origin',signal:controller.signal});
      if(response.status===404){queueEnabled=false;return;}
      var data=await response.json();if(!response.ok)throw new Error(data.error||'读取队列失败');
      if(version!==queueVersion||cid!==selectedConversationId||data.conversation_id!==cid)return;
      queueEnabled=true;
      if(!queueState||JSON.stringify(queueState)!==JSON.stringify(data)){queueState=data;drawQueue();}
    }catch(e){if(e.name!=='AbortError'&&version===queueVersion)drawQueue(e.message);}
    finally{if(queueController===controller){queueController=null;if(queueRefreshPending)refreshQueue();}updateSendAvailability();}
  }

  function restoreQueuedDraft(cid,text,key){
    if(recoveredEdits.has(key))return;
    recoveredEdits.add(key);
    var draft=cid===selectedConversationId?input.value:conversationDrafts[cid]||'';
    var merged=text+(draft?'\n\n'+draft:'');conversationDrafts[cid]=merged;
    try{sessionStorage.setItem(draftKey(cid),merged);}catch(e){}
    try{sessionStorage.setItem('xgent_queue_recovered',JSON.stringify(Array.from(recoveredEdits).slice(-200)));}catch(e){}
    if(cid===selectedConversationId){input.value=merged;syncSendState();input.dispatchEvent(new Event('input'));input.focus();}
  }

  async function changeQueue(item,action){
    var cid=item?.conversation_id||selectedConversationId,key=item?.id||('resume:'+cid),previous=queueActions.get(key);
    if(previous?.busy)return;
    var pending=previous?.action===action?previous:{action,request_id:queueRequestId(),item};pending.busy=true;queueActions.set(key,pending);persistQueueEdits();drawQueue();
    try{
      var data=await api('/api/queue/'+action,{method:'POST',body:JSON.stringify({conversation_id:cid,id:item?.id,
        generation:item?.generation??queueState.generation,revision:item?.revision??queueState.revision,request_id:pending.request_id})});
      if(action==='edit')restoreQueuedDraft(cid,data.text,pending.request_id);
      queueActions.delete(key);persistQueueEdits();await refreshQueue(true);drawQueue();
    }catch(e){pending.busy=false;pending.error=e.message;if(e.status===409&&action==='resume'){queueActions.delete(key);refreshQueue(true);}if(cid===selectedConversationId)drawQueue(e.message);else toast(e.message,true);}
  }

  function persistQueueEdits(){
    try{sessionStorage.setItem('xgent_queue_edits',JSON.stringify(Array.from(queueActions.values()).filter(p=>p.action==='edit').map(p=>({action:p.action,request_id:p.request_id,item:{id:p.item.id,conversation_id:p.item.conversation_id,generation:p.item.generation,revision:p.item.revision}}))));}catch(e){}
  }

  async function sendQueuedText(text){
    var cid=selectedConversationId,draft=input.value;
    if(!textSubmission||textSubmission.cid!==cid||textSubmission.text!==text){textSubmission={cid,text,generation:uiGeneration,request_id:queueRequestId()};}
    var submission=textSubmission;textSubmitting=true;updateSendAvailability();
    try{
      var result=await api('/api/chat',{method:'POST',body:JSON.stringify({text,conversation_id:cid,generation:submission.generation,request_id:submission.request_id})});
      if(cid===selectedConversationId&&input.value===draft){input.value='';input.style.height='auto';conversationDrafts[cid]='';try{sessionStorage.removeItem(draftKey(cid));}catch(e){}}
      else if(cid!==selectedConversationId&&conversationDrafts[cid]===draft){conversationDrafts[cid]='';try{sessionStorage.removeItem(draftKey(cid));}catch(e){}}
      if(!result.queued&&!result.management&&cid===selectedConversationId)makeBubble('user',text);
      textSubmission=null;await refreshQueue(true);triggerHaptic();
    }catch(e){if(e.status>=400&&e.status<500)textSubmission=null;toast(e.message+'；内容已保留，可重试发送。',true);}
    finally{textSubmitting=false;syncSendState();}
  }

  function send() {
    if(queueEnabled&&!pendingFiles.length&&!input.value.trim().startsWith('/')){
      var queuedText=input.value.trim();if(!queuedText||btnSend.disabled)return;
      sendQueuedText(queuedText);return;
    }
    if(viewingPast){historyAnchor=null;viewingPast=false;newFrames=0;newerAvailable=false;updateHistoryState();}
    var text = input.value.trim();
    var hasFiles = pendingFiles.length > 0;
    if ((!text && !hasFiles) || btnSend.disabled) return;

    if (!hasFiles) makeBubble("user", text);
    input.value = "";
    try { sessionStorage.removeItem(draftKey()); } catch(e) {}
    input.style.height = "auto";
    syncSendState();
    hideSuggest();
    setBusy(true);
    triggerHaptic();

    if (hasFiles) {
      sendFiles(text);
      return;
    }

    var isCmd = text.charAt(0) === "/";
    var path = isCmd ? "/api/command" : "/api/chat";
    var submittedCid=selectedConversationId;
    var body = isCmd ? { command: text, conversation_id:submittedCid } : { text: text, conversation_id:submittedCid };
    api(path, { method: "POST", body: JSON.stringify(body) }).catch(function (err) {
      if(submittedCid!==selectedConversationId){
        conversationDrafts[submittedCid]=text;
        try{sessionStorage.setItem(draftKey(submittedCid),text);}catch(e){}
        toast(err.message,true);return;
      }
      makeBubble("sys", "⚠️ " + err.message);
      if (!input.value) { input.value = text; if(!text.startsWith('/')){try { sessionStorage.setItem(draftKey(), text); } catch(e) {}} }
      syncSendState(); setBusy(false);
    });
  }

  function cancelUploads(reason) {
    if (!uploadQueue) return;
    uploadQueue.cancelled = true;
    if (reason && uploadQueue.finish) uploadQueue.finish(reason);
  }

  async function uploadFileChunks(batch, mode, modes, text, key, progress) {
    var pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
    async function control(path, payload){
      var last;
      for(var attempt=0;attempt<4;attempt++){
        var controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
        try{return await api(path,payload===undefined?{signal:controller.signal}:{method:'POST',body:JSON.stringify(payload),signal:controller.signal});}
        catch(error){last=error;if(error.status===507||error.status&&error.status<500)throw error;}
        finally{clearTimeout(timer);}
        await pause(400*(attempt+1));
      }
      throw new Error('暂时无法确认传输状态，重试会从已接收位置继续：'+last.message);
    }
    var transfer=await control('/api/file-transfers/init',{
      idempotency_key:key,conversation_id:batch.conversation_id,mode:mode,text:text,modes:modes,
      files:batch.files.map(file=>({filename:file.name,size:file.size}))
    });
    if(transfer.state==='accepted')return {...transfer,duplicate:true};
    var id=transfer.transfer_id,total=batch.files.reduce((sum,file)=>sum+file.size,0);
    var offsets=transfer.offsets.slice();
    function setProgress(extra){
      var received=offsets.reduce((sum,size)=>sum+size,0)+extra;
      progress.textContent=received>=total?'传输完成，正在保存…':'上传 '+Math.min(99,Math.floor(received/total*100))+'%';
    }
    for(var index=0;index<batch.files.length;index++){
      var file=batch.files[index],failures=0;
      while(offsets[index]<file.size){
        if(batch.cancelled)throw new Error('传输已暂停；重试将从已接收位置继续');
        var start=offsets[index],chunk=file.slice(start,start+transfer.chunk_bytes);
        try{
          var result=await new Promise(function(resolve,reject){
            var request=new XMLHttpRequest();
            request.open('POST','/api/file-transfers/chunk/'+id+'?index='+index+'&offset='+start);
            request.withCredentials=true;request.timeout=90000;
            request.setRequestHeader('Content-Type','application/octet-stream');
            request.upload.onprogress=event=>setProgress(event.loaded);
            request.onerror=()=>reject(new Error('分块网络中断'));
            request.ontimeout=()=>reject(new Error('分块响应超时'));
            request.onload=function(){
              var body;try{body=JSON.parse(request.responseText);}catch(error){body={};}
              if(request.status>=200&&request.status<300)resolve(body);
              else{var failure=new Error(body.error||'HTTP '+request.status);failure.status=request.status;reject(failure);}
            };
            request.send(chunk);
          });
          offsets[index]=result.offset;failures=0;setProgress(0);
        }catch(error){
          if(error.status===507||error.status&&error.status<500&&error.status!==409)throw error;
          progress.textContent='正在核对已接收分块…';
          var state=await control('/api/file-transfers/status/'+id);
          offsets=state.offsets;
          if(state.state==='accepted')return state;
          if(offsets[index]>start){failures=0;continue;}
          if(++failures>=4)throw new Error('连接暂时中断，已接收部分保留，重试可继续上传');
          await pause(500*failures);
        }
      }
    }
    progress.textContent='传输完成，正在保存…';
    await control('/api/file-transfers/complete/'+id,{});
    for(;;){
      var status=await control('/api/file-transfers/status/'+id);
      if(status.state==='accepted')return status;
      if(status.state==='failed')throw new Error(status.error||'服务器保存失败；已传数据保留，可重试');
      if(status.state==='receiving')throw new Error('服务已重启，已传数据保留，请重试确认');
      progress.textContent='传输完成，正在保存校验…';
      await pause(800);
    }
  }

  async function sendFiles(text, selected) {
    var batch = { conversation_id:selectedConversationId, files: selected || pendingFiles.slice(), next: 0, cancelled: false, finish: null, awaitingTurn: false };
    uploadQueue = batch;
    if (selected) { pendingFiles=pendingFiles.filter(function(f){return selected.indexOf(f)<0;});renderPendingFiles(); }
    else clearPendingFiles();
    try {
      // Submit a whole selection once; per-file modes preserve attachment/upload intent.
      if (!batch.cancelled) {
        var f = batch.files[0];
        var modes = batch.files.map(file=>fileModes.get(file)||'attachment');
        var mode = modes.every(value=>value==='server_upload') ? 'server_upload' : 'attachment';
        batch.files.forEach(file=>fileStatuses.set(file,'上传中'));
        fileStatuses.set(f,"上传中");
        var caption = text;
        var entry = makeBubble('user', text);
        batch.entry=entry;
        entry.objectUrls = [];
        setInlineMedia(entry,batch.files.map(function(file){
          var url=URL.createObjectURL(file);entry.objectUrls.push(url);
          return {kind:file.type.startsWith('image/')?'photo':mediaKindFromName(file.name),filename:file.name,download_url:url,local_preview:true};
        }));
        setBusy(true);
        // HTTP 只确认接收，turn_end 才允许下一份上传进入对话核心。
        batch.awaitingTurn = true;
        batch.rejected = false;
        var turnDone = new Promise(function (resolve) { batch.finish = resolve; });
        var fd = new FormData();
        batch.files.forEach(file=>fd.append('file',file,file.name));
        fd.append("text", caption);
        fd.append('modes',JSON.stringify(modes));
        var signature = JSON.stringify([mode,text,modes,batch.files.map(file=>[file.name,file.size,file.lastModified])]);
        var previous = fileBatchKeys.get(f);
        var requestKey = previous?.signature===signature ? previous.key : (crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random());
        byMessageId['upload:'+requestKey]=entry;
        batch.files.forEach(file=>fileBatchKeys.set(file,{signature:signature,key:requestKey}));
        fd.append('idempotency_key',requestKey);
        if(batch.conversation_id)fd.append("conversation_id",batch.conversation_id);
        try {
          var progress=document.createElement('small');progress.className='upload-progress';progress.textContent='正在上传…';
          entry.uploadProgress=progress;entry.bubble.insertBefore(progress,entry.bubble.querySelector('.ts'));
          var body;
          if(batch.files.reduce((sum,file)=>sum+file.size,0)>8*1024*1024){
            body=await uploadFileChunks(batch,mode,modes,caption,requestKey,progress);
          }else{
          var response = await new Promise(function(resolve,reject){
            var request=new XMLHttpRequest();
            request.open('POST',mode==='server_upload'?'/api/server-uploads':'/api/attachments');
            request.withCredentials=true;
            request.upload.onprogress=function(event){
              progress.textContent=event.lengthComputable?(event.loaded>=event.total?'传输完成，正在保存…':'上传 '+Math.round(event.loaded/event.total*100)+'%'):'正在上传…';
            };
            request.upload.onload=function(){progress.textContent='传输完成，正在保存…';};
            request.onerror=()=>reject(new Error('网络中断，文件仍保留，可重试本批文件'));
            request.onload=()=>resolve({ok:request.status>=200&&request.status<300,status:request.status,
              json:async()=>{try{return JSON.parse(request.responseText);}catch(e){return {};}}});
            request.send(fd);
          });
          body = await response.json().catch(function () { return {}; });
          if (!response.ok) throw new Error(body.error || (response.status===413?'反向代理拒绝了上传（HTTP 413），请调整代理请求体限制后重试。':response.status===524?'代理等待服务器响应超时（HTTP 524），请先核对历史；不要重复提交已确认文件。':"HTTP " + response.status));
          }
          progress.textContent='已接收，正在处理';
          if(body.message&&batch.conversation_id===selectedConversationId)putHistoryFrame(body.message,streamEpoch);
          if(body.duplicate){batch.next=batch.files.length;batch.awaitingTurn=false;reconcileAfterTurn=true;return;}
        } catch (err) {
          if(entry.durable){batch.next=batch.files.length;await turnDone;return;}
          batch.awaitingTurn = false;
          removeEntry(entry);
          batch.files.forEach(file=>fileStatuses.set(file,'上传失败：'+err.message));
          batch.error = f.name + " 上传失败：" + err.message;
          return;
        }
        if(batch.rejected){fileStatuses.set(f,'未提交：'+batch.error);return;}
        fileStatuses.set(f,"已接收，处理中");
        batch.next = batch.files.length;
        var turnError=await turnDone;
        if(batch.rejected){batch.next=0;fileStatuses.set(f,'未提交：'+batch.error);return;}
        if (turnError) return;
        batch.finish = null;
      }
    } catch(err) {
      batch.awaitingTurn=false;batch.error=err.message;
    } finally {
      var remaining = batch.files.slice(batch.next);
      if(batch.conversation_id!==selectedConversationId){
        conversationFiles[batch.conversation_id]=remaining.concat(conversationFiles[batch.conversation_id]||[]);
        if(batch.next===0)conversationDrafts[batch.conversation_id]=text;
        uploadQueue=null;
        return;
      }
      pendingFiles = remaining.concat(pendingFiles);
      if (remaining.length) {
        if (batch.next === 0 && !input.value) input.value = text;
      }
      uploadQueue = null;
      renderPendingFiles();
      // Disconnecting the upload queue does not finish an accepted server-side turn.
      if (!batch.awaitingTurn) setBusy(false);
      if ((remaining.length || batch.error || reconcileAfterTurn) && !currentConnState) {
        reconcileAfterTurn = false;
        await resync();
      }
      if (batch.error) makeBubble("sys", "⚠️ " + batch.error);
      if (remaining.length) toast("尚未发送的 " + remaining.length + " 个文件已保留在输入区", true);
    }
  }

  var suggestMatches = [];
  function markActive() {
    var items = suggest.querySelectorAll(".cmd-item");
    Array.prototype.forEach.call(items, function (it, i) {
      it.classList.toggle("active", i === suggestIndex);
    });
  }
  function showSuggest(q) {
    var prefix = q.toLowerCase();
    var matches = FLAT_COMMANDS.filter(function (c) { return c.cmd.toLowerCase().indexOf(prefix) === 0; });
    if (!matches.length) { hideSuggest(); return; }
    suggest.innerHTML = "";
    suggestMatches = matches.slice(0, 6);
    suggestMatches.forEach(function (c, i) {
      var item = document.createElement("div");
      item.className = "cmd-item";
      item.innerHTML = '<span class="c-cmd">' + escapeHtml(c.cmd) + '</span><span class="c-desc">' + escapeHtml(c.desc) + '</span>';
      item.addEventListener("click", function () {
        input.value = c.cmd + " ";
        input.focus();
        hideSuggest();
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 180) + "px";
        syncSendState();
      });
      item.addEventListener("mouseenter", function () {
        suggestIndex = i;
        markActive();
      });
      suggest.appendChild(item);
    });
    suggestIndex = 0;
    markActive();
    var foot = document.createElement("div");
    foot.className = "cmd-foot";
    foot.innerHTML = '<span>↑↓ 选择 · Tab 补全 · Enter 执行</span><span>Esc 关闭</span>';
    suggest.appendChild(foot);
    suggest.classList.remove("hidden");
  }
  function hideSuggest() { suggest.classList.add("hidden"); suggestIndex = -1; suggestMatches = []; }

  var searchBar = document.getElementById("search-bar");
  var searchInput = document.getElementById("search-input");
  var searchCount = document.getElementById("search-count");
  var searchMatches = [];
  var currentMatchIdx = -1;

  function toggleSearch() {
    var isOpen = searchBar.classList.contains("open");
    if (isOpen) {
      closeSearch();
    } else {
      searchBar.classList.add("open");
      document.getElementById("btn-search").classList.add("active");
      searchInput.focus();
      if (searchInput.value.trim()) runSearch();
    }
  }

  function closeSearch() {
    searchBar.classList.remove("open");
    document.getElementById("btn-search").classList.remove("active");
    clearSearchHighlights();
    searchMatches = [];
    currentMatchIdx = -1;
    searchCount.textContent = "0/0";
  }

  function clearSearchHighlights() {
    var marks = log.querySelectorAll(".highlight-match");
    Array.prototype.forEach.call(marks, function (m) {
      var parent = m.parentNode;
      if (parent) {
        parent.replaceChild(document.createTextNode(m.textContent), m);
        parent.normalize();
      }
    });
  }

  function runSearch() {
    clearSearchHighlights();
    searchMatches = [];
    currentMatchIdx = -1;
    var q = searchInput.value.trim();
    if (!q) { searchCount.textContent = "0/0"; return; }

    var walker = document.createTreeWalker(log, NodeFilter.SHOW_TEXT, null, false);
    var textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);

    var regex = new RegExp("(" + q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
    textNodes.forEach(function (node) {
      regex.lastIndex = 0;
      if (!node.nodeValue || !regex.test(node.nodeValue)) return;
      if (node.parentNode && (node.parentNode.classList.contains("ts") || node.parentNode.tagName === "BUTTON")) return;
      var frag = document.createDocumentFragment();
      var last = 0, match;
      regex.lastIndex = 0;
      while ((match = regex.exec(node.nodeValue)) !== null) {
        frag.appendChild(document.createTextNode(node.nodeValue.slice(last, match.index)));
        var mark = document.createElement("mark");
        mark.className = "highlight-match";
        mark.textContent = match[0];
        frag.appendChild(mark);
        last = match.index + match[0].length;
      }
      frag.appendChild(document.createTextNode(node.nodeValue.slice(last)));
      node.parentNode.replaceChild(frag, node);
    });

    searchMatches = Array.prototype.slice.call(log.querySelectorAll(".highlight-match"));
    searchCount.textContent = searchMatches.length ? "1/" + searchMatches.length : "0/0";
    if (searchMatches.length) jumpToMatch(0);
  }

  function jumpToMatch(idx) {
    if (!searchMatches.length) return;
    if (currentMatchIdx >= 0 && searchMatches[currentMatchIdx]) {
      searchMatches[currentMatchIdx].classList.remove("current");
    }
    currentMatchIdx = (idx + searchMatches.length) % searchMatches.length;
    var target = searchMatches[currentMatchIdx];
    target.classList.add("current");
    searchCount.textContent = (currentMatchIdx + 1) + "/" + searchMatches.length;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  document.getElementById("btn-search").addEventListener("click", toggleSearch);
  document.getElementById("search-close").addEventListener("click", closeSearch);
  document.getElementById("search-prev").addEventListener("click", function () { jumpToMatch(currentMatchIdx - 1); });
  document.getElementById("search-next").addEventListener("click", function () { jumpToMatch(currentMatchIdx + 1); });
  searchInput.addEventListener("input", runSearch);
  searchInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      if (e.shiftKey) jumpToMatch(currentMatchIdx - 1);
      else jumpToMatch(currentMatchIdx + 1);
    } else if (e.key === "Escape") {
      closeSearch();
    }
  });

  window.addEventListener("paste", function (e) {
    if (!e.clipboardData || !e.clipboardData.items) return;
    var items = e.clipboardData.items;
    var caught = false;
    for (var i = 0; i < items.length; i++) {
      if (items[i].type.indexOf("image") !== -1) {
        var file = items[i].getAsFile();
        if (file) {
          pendingFiles.push(file);
          caught = true;
        }
      }
    }
    if (caught) {
      renderPendingFiles();
      toast("已从剪贴板捕获图片截图");
      triggerHaptic();
    }
  });

  var dropOverlay = document.getElementById("drop-overlay");
  var dragCounter = 0;
  window.addEventListener("dragenter", function (e) {
    e.preventDefault();
    dragCounter++;
    dropOverlay.classList.add("active");
  });
  window.addEventListener("dragleave", function (e) {
    e.preventDefault();
    dragCounter--;
    if (dragCounter <= 0) {
      dragCounter = 0;
      dropOverlay.classList.remove("active");
    }
  });
  window.addEventListener("dragover", function (e) { e.preventDefault(); });
  window.addEventListener("drop", function (e) {
    e.preventDefault();
    dragCounter = 0;
    dropOverlay.classList.remove("active");
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      for (var i = 0; i < e.dataTransfer.files.length; i++) {
        pendingFiles.push(e.dataTransfer.files[i]);
      }
      renderPendingFiles();
      toast("已添加 " + e.dataTransfer.files.length + " 个待发送文件");
      triggerHaptic();
    }
  });

  var SETTING_FIELDS = [
    { key: "thinking_level", label: "思考深度 / 推理等级", type: "select" },
    { key: "stream_mode", label: "打字机流式输出", type: "bool" },
    { key: "hide_protocol_blocks", label: "折叠协议代码块", type: "bool" },
    { key: "agent_mode", label: "Agent 智能体模式", type: "bool" },
    { key: "text_stitch_mode", label: "文字拼接模式", type: "select" },
    { key: "global_depth", label: "全局记忆轮数", type: "number", min: 1 },
    { key: "agent_max_iterations", label: "Agent 最大迭代轮数", type: "number", min: 1 },
    { key: "stream_timeout", label: "流式回复超时(秒, 0=不限)", type: "number", min: 0 },
    { key: "agent_command_timeout", label: "命令等待超时(秒)", type: "number", min: 5 },
    { key: "idle_message_interval", label: "空闲提醒间隔(秒, 0=关闭)", type: "number", min: 0 },
    { key: "smart_match_threshold", label: "智能匹配阈值(%)", type: "number", min: 0 },
    { key: "chat_model", label: "默认对话模型 (🟢 有效 / 🔴 失效)", type: "select" },
    { key: "disabled_skills", label: "Skill 插件管理", type: "skills" },
    { key: "stats_auto_merge", label: "统计：相同模型自动合并", type: "bool" },
    { key: "stats_metric", label: "统计：默认指标", type: "select" },
    { key: "model_price_table", label: "💵 模型价格表 (每百万 Token / USD)", type: "price_table" },
    { key: "model_merge_map", label: "🔗 手动模型合并表", type: "merge_map" }
  ];

  function saveSetting(key, value) {
    return api("/api/config", { method: "POST", body: JSON.stringify({ key: key, value: value }) })
      .then(function () { toast("设置已保存"); triggerHaptic("success"); })
      .catch(function (err) { toast("保存失败：" + err.message); loadSettings(); });
  }

  function renderPriceTableEditor(row, initData) {
    var draft = {};
    function loadDraft(d) {
      draft = {};
      if (d && typeof d === "object") {
        Object.keys(d).forEach(function (k) {
          var v = d[k] || {};
          draft[k] = { input: Number(v.input || 0), output: Number(v.output || 0), cached: Number(v.cached || 0) };
        });
      }
    }
    loadDraft(initData);
    var box = document.createElement("div");
    box.className = "ptable";
    box.style.cssText = "border:1px solid var(--line);border-radius:var(--r-sm);padding:10px;margin-top:6px;width:100%;";

    function refresh() {
      box.innerHTML = "";
      var keys = Object.keys(draft).sort();
      if (!keys.length) {
        var empty = document.createElement("div");
        empty.style.cssText = "color:var(--text-3);font-size:13px;padding:4px 0;";
        empty.textContent = "（未配置独立价格，按 default 计费）";
        box.appendChild(empty);
      } else {
        var tbl = document.createElement("table");
        tbl.style.cssText = "width:100%;border-collapse:collapse;font-size:13px;";
        tbl.innerHTML = '<thead><tr style="color:var(--text-3);border-bottom:1px solid var(--line);">' +
          '<th style="text-align:left;padding:6px 4px;">模型</th><th>输入($)</th><th>输出($)</th><th>缓存($)</th><th></th></tr></thead>';
        var tbody = document.createElement("tbody");
        keys.forEach(function (k) {
          var tr = document.createElement("tr");
          tr.style.borderBottom = "1px solid var(--line)";
          var tdName = document.createElement("td");
          tdName.style.cssText = "padding:6px 4px;font-weight:600;";
          tdName.textContent = k;
          tr.appendChild(tdName);
          ["input", "output", "cached"].forEach(function (f) {
            var td = document.createElement("td");
            td.style.textAlign = "center";
            var inp = document.createElement("input");
            inp.type = "number"; inp.step = "0.01"; inp.min = "0";
            inp.value = draft[k][f];
            inp.style.cssText = "width:64px;padding:4px;border:1px solid var(--line);border-radius:4px;background:var(--surface-2);color:var(--text);text-align:right;";
            inp.addEventListener("change", function () {
              var v = Number(inp.value);
              draft[k][f] = (isNaN(v) || v < 0) ? 0 : v;
            });
            td.appendChild(inp);
            tr.appendChild(td);
          });
          var tdDel = document.createElement("td");
          tdDel.style.textAlign = "right";
          var del = document.createElement("button");
          del.type = "button"; del.textContent = "✕";
          del.style.cssText = "background:var(--danger-soft);border:none;color:var(--danger);border-radius:4px;padding:3px 7px;cursor:pointer;";
          del.addEventListener("click", function () { delete draft[k]; refresh(); });
          tdDel.appendChild(del);
          tr.appendChild(tdDel);
          tbody.appendChild(tr);
        });
        tbl.appendChild(tbody);
        box.appendChild(tbl);
      }

      var add = document.createElement("div");
      add.style.cssText = "display:flex;gap:6px;margin-top:10px;flex-wrap:wrap;align-items:center;";
      var nameInp = document.createElement("input");
      nameInp.type = "text"; nameInp.placeholder = "模型名 (如 gemini-2.5 或 default)";
      nameInp.style.cssText = "flex:1;min-width:160px;padding:6px 10px;border:1px solid var(--line);border-radius:var(--r-xs);background:var(--surface-2);color:var(--text);";
      var inInp = document.createElement("input");
      inInp.type = "number"; inInp.step = "0.01"; inInp.placeholder = "输入"; inInp.style.cssText = "width:60px;padding:6px;border:1px solid var(--line);border-radius:var(--r-xs);background:var(--surface-2);color:var(--text);";
      var outInp = document.createElement("input");
      outInp.type = "number"; outInp.step = "0.01"; outInp.placeholder = "输出"; outInp.style.cssText = "width:60px;padding:6px;border:1px solid var(--line);border-radius:var(--r-xs);background:var(--surface-2);color:var(--text);";
      var cacheInp = document.createElement("input");
      cacheInp.type = "number"; cacheInp.step = "0.01"; cacheInp.placeholder = "缓存"; cacheInp.style.cssText = "width:60px;padding:6px;border:1px solid var(--line);border-radius:var(--r-xs);background:var(--surface-2);color:var(--text);";
      var addBtn = document.createElement("button");
      addBtn.type = "button"; addBtn.textContent = "+ 添加";
      addBtn.style.cssText = "padding:6px 12px;border:none;border-radius:var(--r-xs);background:var(--accent);color:#fff;font-weight:600;cursor:pointer;";
      addBtn.addEventListener("click", function () {
        var name = nameInp.value.trim();
        if (!name) { toast("请填写模型名称"); return; }
        draft[name] = { input: Number(inInp.value) || 0, output: Number(outInp.value) || 0, cached: Number(cacheInp.value) || 0 };
        refresh();
      });
      add.appendChild(nameInp); add.appendChild(inInp); add.appendChild(outInp); add.appendChild(cacheInp); add.appendChild(addBtn);
      box.appendChild(add);

      var save = document.createElement("button");
      save.type = "button"; save.textContent = "💾 保存价格表配置";
      save.style.cssText = "margin-top:12px;padding:8px 16px;border:none;border-radius:var(--r-sm);background:var(--ok);color:#fff;font-weight:600;cursor:pointer;";
      save.addEventListener("click", function () {
        save.disabled = true; save.textContent = "保存中…";
        saveSetting("model_price_table", draft).finally(function () {
          save.textContent = "💾 保存价格表配置"; save.disabled = false;
        });
      });
      box.appendChild(save);
    }
    refresh();
    return box;
  }

  function renderMergeMapEditor(row, initData) {
    var draft = {};
    function loadDraft(d) {
      draft = {};
      if (d && typeof d === "object") {
        Object.keys(d).forEach(function (k) {
          var v = d[k];
          if (Array.isArray(v)) draft[k] = v.map(String);
        });
      }
    }
    loadDraft(initData);
    var box = document.createElement("div");
    box.className = "mmap";
    box.style.cssText = "border:1px solid var(--line);border-radius:var(--r-sm);padding:10px;margin-top:6px;width:100%;";

    function refresh() {
      box.innerHTML = "";
      var keys = Object.keys(draft).sort();
      if (!keys.length) {
        var empty = document.createElement("div");
        empty.style.cssText = "color:var(--text-3);font-size:13px;padding:4px 0;";
        empty.textContent = "（空。手动合并映射优先于自动交集合并）";
        box.appendChild(empty);
      } else {
        var tbl = document.createElement("table");
        tbl.style.cssText = "width:100%;border-collapse:collapse;font-size:13px;";
        tbl.innerHTML = '<thead><tr style="color:var(--text-3);border-bottom:1px solid var(--line);"><th style="text-align:left;padding:6px 4px;">规范模型名</th><th style="text-align:left;">合并成员 (逗号分隔)</th><th></th></tr></thead>';
        var tbody = document.createElement("tbody");
        keys.forEach(function (k) {
          var tr = document.createElement("tr");
          tr.style.borderBottom = "1px solid var(--line)";
          var tdName = document.createElement("td");
          tdName.style.padding = "6px 4px";
          var nameInp = document.createElement("input");
          nameInp.type = "text"; nameInp.value = k;
          nameInp.style.cssText = "width:100%;padding:4px;border:1px solid var(--line);border-radius:4px;background:var(--surface-2);color:var(--text);";
          var oldName = k;
          nameInp.addEventListener("change", function () {
            var nn = nameInp.value.trim();
            if (!nn || nn === oldName) return;
            draft[nn] = draft[oldName];
            delete draft[oldName];
            oldName = nn;
          });
          tdName.appendChild(nameInp);
          tr.appendChild(tdName);

          var tdMem = document.createElement("td");
          tdMem.style.padding = "6px 4px";
          var memInp = document.createElement("input");
          memInp.type = "text"; memInp.value = (draft[k] || []).join(", ");
          memInp.style.cssText = "width:100%;padding:4px;border:1px solid var(--line);border-radius:4px;background:var(--surface-2);color:var(--text);";
          memInp.addEventListener("change", function () {
            draft[oldName] = memInp.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
          });
          tdMem.appendChild(memInp);
          tr.appendChild(tdMem);

          var tdDel = document.createElement("td");
          tdDel.style.textAlign = "right";
          var del = document.createElement("button");
          del.type = "button"; del.textContent = "✕";
          del.style.cssText = "background:var(--danger-soft);border:none;color:var(--danger);border-radius:4px;padding:3px 7px;cursor:pointer;";
          del.addEventListener("click", function () { delete draft[oldName]; refresh(); });
          tdDel.appendChild(del);
          tr.appendChild(tdDel);
          tbody.appendChild(tr);
        });
        tbl.appendChild(tbody);
        box.appendChild(tbl);
      }

      var add = document.createElement("div");
      add.style.cssText = "display:flex;gap:6px;margin-top:10px;flex-wrap:wrap;";
      var nameInp = document.createElement("input");
      nameInp.type = "text"; nameInp.placeholder = "规范名 (如 claude-3-7)";
      nameInp.style.cssText = "flex:1;min-width:140px;padding:6px 10px;border:1px solid var(--line);border-radius:var(--r-xs);background:var(--surface-2);color:var(--text);";
      var memInp = document.createElement("input");
      memInp.type = "text"; memInp.placeholder = "成员 (如 1-claude-3-7, 2-claude-3-7)";
      memInp.style.cssText = "flex:2;min-width:200px;padding:6px 10px;border:1px solid var(--line);border-radius:var(--r-xs);background:var(--surface-2);color:var(--text);";
      var addBtn = document.createElement("button");
      addBtn.type = "button"; addBtn.textContent = "+ 添加";
      addBtn.style.cssText = "padding:6px 12px;border:none;border-radius:var(--r-xs);background:var(--accent);color:#fff;font-weight:600;cursor:pointer;";
      addBtn.addEventListener("click", function () {
        var name = nameInp.value.trim();
        var parts = memInp.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        if (!name || !parts.length) { toast("请填写规范名和至少一个成员"); return; }
        draft[name] = parts;
        refresh();
      });
      add.appendChild(nameInp); add.appendChild(memInp); add.appendChild(addBtn);
      box.appendChild(add);

      var save = document.createElement("button");
      save.type = "button"; save.textContent = "💾 保存合并映射";
      save.style.cssText = "margin-top:12px;padding:8px 16px;border:none;border-radius:var(--r-sm);background:var(--ok);color:#fff;font-weight:600;cursor:pointer;";
      save.addEventListener("click", function () {
        save.disabled = true; save.textContent = "保存中…";
        saveSetting("model_merge_map", draft).finally(function () {
          save.textContent = "💾 保存合并映射"; save.disabled = false;
        });
      });
      box.appendChild(save);
    }
    refresh();
    return box;
  }

  function loadSettings() {
    return api("/api/config").then(function (data) {
      settingsRows.innerHTML = "";
      SETTING_FIELDS.forEach(function (field) {
        if (!(field.key in data.values)) return;
        var row = document.createElement("div");
        row.className = "row";
        var label = document.createElement("label");
        label.textContent = field.label;
        row.appendChild(label);

        if (field.type === "bool") {
          var sw = document.createElement("button");
          sw.type = "button"; sw.className = "switch";
          sw.setAttribute("role", "switch");
          sw.innerHTML = '<span class="knob"></span>';
          var on = !data.values[field.key];
          sw.classList.toggle("on", on);
          sw.setAttribute("aria-checked", on ? "true" : "false");
          sw.addEventListener("click", function () {
            var next = !sw.classList.contains("on");
            sw.classList.toggle("on", next);
            sw.setAttribute("aria-checked", next ? "true" : "false");
            saveSetting(field.key, next);
          });
          row.appendChild(sw);
        } else if (field.type === "select") {
          var wrap = document.createElement("div");
          wrap.className = "select-wrap";
          var sel = document.createElement("select");
          (data.options[field.key] || []).forEach(function (opt) {
            var o = document.createElement("option"); o.value = opt.value; o.textContent = opt.label; sel.appendChild(o);
          });
          sel.value = String(data.values[field.key]);
          sel.addEventListener("change", function () {
            saveSetting(field.key, sel.value);
            if (field.key === "chat_model") {
              var selOpt = sel.options[sel.selectedIndex];
              if (selOpt) document.getElementById("model-pill").textContent = selOpt.textContent;
            }
          });
          wrap.appendChild(sel);
          wrap.insertAdjacentHTML("beforeend", svgIcon("chev"));
          row.appendChild(wrap);
        } else if (field.type === "skills") {
          row.classList.add("skill-settings-row");
          var skills = data.options.skill_list || [];
          var disabled = data.values.disabled_skills || [];
          var disabledSet = {};
          disabled.forEach(function (p) { disabledSet[p] = true; });
          var hiddenSet = {};
          (data.values.hidden_skills || []).forEach(function (p) { hiddenSet[p] = true; });
          var skillBox = document.createElement("div");
          skillBox.className = "skill-list";
          skills.forEach(function (sk) {
            var item = document.createElement("div");
            item.className = "skill-item";
            var name = document.createElement("span");
            name.className = "skill-name";
            name.textContent = (sk.source === "private" ? "🔒 " : "📦 ") + sk.label;
            item.dataset.path = sk.path;
            item.appendChild(name);
            var stateButton = document.createElement("button");
            stateButton.type = "button";
            stateButton.className = "skill-state";
            var states = {
              enabled: { label: "启用", next: "disabled", hint: "关闭后保留名字和路径" },
              disabled: { label: "已关闭", next: "hidden", hint: "彻底隐藏该技能条目" },
              hidden: { label: "已隐藏", next: "enabled", hint: "启用该技能" }
            };
            function showState(state) {
              stateButton.dataset.state = state;
              stateButton.innerHTML = '<span class="skill-state-dot" aria-hidden="true"></span><span>' + states[state].label + '</span>';
              stateButton.title = states[state].hint;
              stateButton.setAttribute("aria-label", sk.label + "：" + states[state].label + "；" + states[state].hint);
            }
            showState(hiddenSet[sk.path] ? "hidden" : (disabledSet[sk.path] ? "disabled" : "enabled"));
            stateButton.addEventListener("click", function () {
              var next = states[stateButton.dataset.state].next;
              stateButton.disabled = true;
              api("/api/config", { method: "POST", body: JSON.stringify({
                key: "skill_state", value: { path: sk.path, state: next }
              }) }).then(function () { showState(next); toast("已保存"); })
                .catch(function (err) { toast("保存失败：" + err.message); })
                .finally(function () { stateButton.disabled = false; });
            });
            item.appendChild(stateButton);
            skillBox.appendChild(item);
          });
          if (!skills.length) skillBox.textContent = "暂无安装的 Skill 插件";
          row.appendChild(skillBox);
        } else if (field.type === "price_table") {
          row.appendChild(renderPriceTableEditor(row, data.values[field.key] || {}));
        } else if (field.type === "merge_map") {
          row.appendChild(renderMergeMapEditor(row, data.values[field.key] || {}));
        } else {
          var num = document.createElement("input");
          num.type = "number";
          if (field.min != null) num.min = field.min;
          if (field.max != null) num.max = field.max;
          num.value = data.values[field.key];
          num.addEventListener("change", function () { saveSetting(field.key, Number(num.value)); });
          row.appendChild(num);
        }
        settingsRows.appendChild(row);
      });
    });
  }

  function boot() {
    loggedOut=false;
    window.dispatchEvent(new CustomEvent("xgent-authenticated"));
    loginDlg.close();
    resync().catch(function(){});
    connectStream();
  }
  function tryLogin(payload) { return api("/api/login", { method: "POST", body: JSON.stringify(payload) }); }

  api("/api/session").then(function (data) {
    if (data.authenticated) { boot(); return; }
    var initData = tg && tg.initData ? tg.initData : "";
    if (initData) {
      return tryLogin({ init_data: initData }).then(boot).catch(function () {
        loginErr.textContent = "Telegram 免密登录失败，请输入访问密码";
        loginDlg.showModal();
      });
    }
    loginSub.textContent = "请输入访问密码以继续使用 XGent Web";
    loginDlg.showModal();
  }).catch(function () { loginDlg.showModal(); });

  document.getElementById("btn-login").addEventListener("click", function () {
    loginErr.textContent = "";
    tryLogin({ password: document.getElementById("password").value })
      .then(boot).catch(function (err) { loginErr.textContent = err.message; });
  });
  document.getElementById("password").addEventListener("keydown", function (e) {
    if (e.key === "Enter") document.getElementById("btn-login").click();
  });

  btnSend.addEventListener("click", send);
  bindContentClicks(log);
  if (IS_TOUCH) {
    input.setAttribute("enterkeyhint", "enter");   // 软键盘显示"换行"而不是"发送/前往"
    btnSend.title = "发送消息";                     // 桌面文案是"发送消息 (Enter)"，触屏上不成立
  }
  input.addEventListener("keydown", function (e) {
    if(e.key === 'Enter' && e.altKey && !e.isComposing) {
      e.preventDefault(); input.setRangeText('\n',input.selectionStart,input.selectionEnd,'end');
      input.dispatchEvent(new Event('input',{bubbles:true}));return;
    }
    if (suggest.classList.contains("hidden")) {
      if (e.key === "Enter" && !e.shiftKey && !e.altKey && !IS_TOUCH && !e.isComposing) { e.preventDefault(); send(); }
      return;
    }
    var items = suggest.querySelectorAll(".cmd-item");
    if (e.key === "ArrowDown") { e.preventDefault(); suggestIndex = Math.min(suggestIndex + 1, items.length - 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); suggestIndex = Math.max(suggestIndex - 1, 0); }
    else if (e.key === "Tab") {
      e.preventDefault();
      if (suggestIndex >= 0 && items[suggestIndex]) items[suggestIndex].click();
    } else if (e.key === "Enter") {
      // 明确的 /命令选择：桌面/触屏回车都直接执行当前候选。
      // Shift/Alt+Enter 和 IME 确认仍不触发发送；普通触屏正文回车仍换行。
      if (e.shiftKey || e.altKey || e.isComposing) { hideSuggest(); return; }
      e.preventDefault();
      var selected = suggestMatches[suggestIndex >= 0 ? suggestIndex : 0];
      if (selected) input.value = selected.cmd;
      hideSuggest(); syncSendState(); send(); return;
    } else if (e.key === "Escape") { hideSuggest(); return; }
    markActive();
  });

  input.addEventListener("input", function () {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 180) + "px";
    syncSendState();
    var val = input.value;
    var slash = val.lastIndexOf("/");
    if (slash === 0 && !/\s/.test(val)) showSuggest(val);
    else hideSuggest();
  });
  syncSendState();

  btnStop.addEventListener("click", function () {
    triggerHaptic("warning");
    cancelUploads();
    api("/api/stop", { method: "POST" }).catch(function (err) { toast("停止失败：" + err.message, true); });
  });
  window.addEventListener("beforeunload", function (event) {
    if (!uploadQueue) return;
    event.preventDefault();
    event.returnValue = "";
  });
  btnAttach.addEventListener("click", function () { fileInput.click(); });
  document.getElementById('btn-server-upload').addEventListener('click',()=>serverUploadInput.click());
  serverUploadInput.addEventListener('change',function(){
    Array.from(serverUploadInput.files).forEach(file=>{fileModes.set(file,'server_upload');pendingFiles.push(file);});
    serverUploadInput.value='';renderPendingFiles();
  });

  fileInput.addEventListener("change", function () {
    for (var i = 0; i < fileInput.files.length; i++) pendingFiles.push(fileInput.files[i]);
    fileInput.value = "";
    renderPendingFiles();
  });

  function renderPendingFiles() {
    pendingFilesEl.innerHTML = "";
    pendingFiles.forEach(function (f, idx) {
      var chip = document.createElement("div");
      chip.className = "pending-file";
      var name = document.createElement("span");
      name.textContent = (fileModes.get(f)==='server_upload'?'↑ 上传：':'📎 附件：') + f.name;
      var del = document.createElement("button");
      del.className = "pf-del";
      del.type = "button";
      del.textContent = "×";
      del.addEventListener("click", function () {
        pendingFiles.splice(idx, 1);
        renderPendingFiles();
      });
      chip.appendChild(name);
      chip.appendChild(del);
      var fileState=fileStatuses.get(f);
      if(fileState){var note=document.createElement('span');note.textContent=fileState;note.className='wb-upload-status';chip.appendChild(note);}
      if(fileState && fileState.indexOf('上传失败')===0){
        var retry=document.createElement('button');retry.type='button';retry.className='wb-btn';retry.textContent='重试本批文件';
        retry.onclick=function(){if(!uploadQueue&&!btnSend.disabled)sendFiles(input.value);};chip.appendChild(retry);
      }
      pendingFilesEl.appendChild(chip);
    });
    pendingFilesEl.classList.toggle("hidden", pendingFiles.length === 0);
    syncSendState();
  }

  function clearPendingFiles() {
    pendingFiles = [];
    renderPendingFiles();
  }

  var chips = document.querySelectorAll("#empty-state .chip");
  Array.prototype.forEach.call(chips, function (chip) {
    chip.addEventListener("click", function () {
      input.value = chip.dataset.cmd + " ";
      input.focus();
      syncSendState();
      triggerHaptic();
    });
  });

  log.addEventListener("scroll", function () {
    document.getElementById('wb-chat').classList.toggle('history-top',log.scrollTop<8&&!!historyCursor);
    if (isNearBottom()) hideScrollBtn(); else showScrollBtn();
    scheduleHistoryPrefetch();
  });
  new ResizeObserver(scheduleHistoryPrefetch).observe(log);
  scrollBtn.addEventListener("click", function () { scrollDown(); triggerHaptic(); });

  // These essentials belong to the standalone chat entry, not to the optional
  // admin module graph (a failed usage/settings import must not disable logout).
  var actionDialog=document.getElementById('wb-dialog'),actionReturn=null,actionRequest=null,actionRequestPath=null;
  var commandPanel=document.getElementById('composer-commands'),commandTrigger=null,commandSequence=0;
  function actionNode(tag,text,cls){var node=document.createElement(tag);if(text!=null)node.textContent=text;if(cls)node.className=cls;return node;}
  function actionButton(text,fn,cls){var node=actionNode('button',text,'wb-btn '+(cls||''));node.type='button';node.addEventListener('click',fn);return node;}
  function actionMessage(text,error){var node=actionNode('div',text,error?'wb-error':'wb-loading');node.setAttribute('role',error?'alert':'status');return node;}
  function showAction(title,content){
    closeCommands(false);hideSuggest();
    if(window.XGentWorkbench){window.XGentWorkbench.dialog(title,content);return;}
    actionReturn=document.activeElement;actionDialog.dataset.chatFallback='1';
    document.getElementById('wb-dialog-title').textContent=title;
    document.getElementById('wb-dialog-body').replaceChildren(content);
    if(!actionDialog.open)actionDialog.showModal();
    requestAnimationFrame(()=>content.querySelector('input,button')?.focus());
  }
  function closeAction(){
    if(actionRequest){actionRequest.abort();actionRequest=null;}
    if(actionDialog.dataset.chatFallback){
      delete actionDialog.dataset.chatFallback;actionDialog.close();
      document.getElementById('wb-dialog-body').replaceChildren();actionReturn?.focus();
    }else if(window.XGentWorkbench)window.XGentWorkbench.closeDialog();
  }
  async function actionApi(path,options){
    if(actionRequest)actionRequest.abort();
    var controller=new AbortController();actionRequest=controller;actionRequestPath=path;
    var timedOut=false,timer=setTimeout(()=>{timedOut=true;controller.abort();},12000);
    try{return await api(path,Object.assign({},options,{signal:controller.signal}));}
    catch(err){if(timedOut)throw new Error('请求超时，请检查连接后重试。');throw err;}
    finally{clearTimeout(timer);if(actionRequest===controller){actionRequest=null;actionRequestPath=null;}}
  }
  var loggingOut=false;
  async function logoutAction(){
    if(loggingOut)return;
    if(window.XGentWorkbench&&!window.XGentWorkbench.beforeLogout())return;
    loggingOut=true;
    var box=actionNode('div');box.append(actionMessage('正在退出当前登录…'));
    try{
      showAction('退出登录',box);
      await actionApi('/api/logout',{method:'POST'});
      closeAction();window.dispatchEvent(new Event('xgent-auth-expired'));
      // Chat still owns login if the optional workbench did not load.
      if(!loginDlg.open)loginDlg.showModal();
    }catch(err){if(err.name!=='AbortError'){if(box.isConnected)box.replaceChildren(actionMessage('退出失败：'+err.message,true),actionButton('重试退出',logoutAction));else toast('退出失败：'+err.message,true);}}
    finally{loggingOut=false;}
  }
  function closeCommands(restoreFocus){
    if(!commandPanel||commandPanel.hidden)return;
    commandSequence++;commandPanel.hidden=true;
    if(actionRequestPath==='/api/workbench/bootstrap'&&actionRequest){actionRequest.abort();actionRequest=null;actionRequestPath=null;}
    ['btn-composer-menu'].forEach(id=>document.getElementById(id)?.setAttribute('aria-expanded','false'));
    if(restoreFocus&&commandTrigger?.isConnected)commandTrigger.focus({preventScroll:true});
  }
  async function openCommands(){
    if(!commandPanel.hidden){closeCommands(true);return;}
    if(actionDialog.open)return;
    commandTrigger=document.activeElement;
    if(window.XGentWorkbench){if(!await window.XGentWorkbench.prepareChat())return;}
    else {document.body.dataset.page='chat';location.hash='/chat';}
    hideSuggest();
    const sequence=++commandSequence;
    commandPanel.hidden=false;
    ['btn-composer-menu'].forEach(id=>document.getElementById(id)?.setAttribute('aria-expanded','true'));
    var list=actionNode('div',null,'composer-command-list'),feedback=actionNode('div');feedback.setAttribute('aria-live','polite');
    var header=actionNode('div',null,'composer-command-head');
    var dismiss=actionButton('×',()=>closeCommands(true));dismiss.id='composer-commands-close';dismiss.setAttribute('aria-label','收起命令列表');
    header.append(actionNode('span','命令'),dismiss);
    commandPanel.replaceChildren(header,feedback,list);
    positionCommands();
    var commands=window.XGentWorkbench?.commands()||null;
    function execute(cmd){
      var error=window.XGentChat.commandError();if(error){feedback.replaceChildren(actionMessage(error,true));return;}
      closeCommands(false);
      if(['/clear','/clear_memory','/restart','/update'].includes(cmd)){
        if(window.XGentWorkbench){window.XGentWorkbench.confirmCommand(cmd);return;}
        if(!confirm('此命令可能改变共享数据或重启服务，是否执行 '+cmd+'？'))return;
      }
      window.XGentChat.command(cmd);
    }
    function draw(){
      var items=(commands||[]).filter(c=>c&&typeof c.cmd==='string'&&typeof c.desc==='string').slice()
        .sort((a,b)=>a.cmd==='/start'?-1:b.cmd==='/start'?1:0);
      list.replaceChildren();items.forEach(c=>{var btn=actionButton('',()=>execute(c.cmd));btn.className='composer-command';btn.append(actionNode('strong',c.cmd),actionNode('span',c.desc));list.append(btn);});
      if(!items.length)list.append(actionMessage('服务端暂时没有可用命令。'));
      // A touch tap must not open the software keyboard or move focus away from
      // the composer. Keyboard users can immediately traverse the list.
      if(!IS_TOUCH)list.querySelector('button')?.focus({preventScroll:true});
    }
    async function load(){
      list.replaceChildren(actionMessage('正在读取命令列表…'));
      try{var data=await actionApi('/api/workbench/bootstrap');if(!Array.isArray(data.commands))throw new Error('服务未返回命令注册表');commands=data.commands;if(sequence===commandSequence&&!commandPanel.hidden)draw();}
      catch(err){if(err.name!=='AbortError'&&sequence===commandSequence&&!commandPanel.hidden)list.replaceChildren(actionMessage('命令列表读取失败：'+err.message,true),actionButton('重新读取',load));}
    }
    if(commands)draw();else await load();
  }
  function positionCommands(){
    if(!commandPanel||commandPanel.hidden)return;
    var viewport=window.visualViewport,top=viewport?viewport.offsetTop:0;
    var available=document.getElementById('composer').getBoundingClientRect().top-Math.max(top+8,document.getElementById('header').getBoundingClientRect().bottom+8)-8;
    commandPanel.style.maxHeight=Math.max(0,Math.min(320,available))+'px';
  }
  document.addEventListener('pointerdown',function(e){
    if(commandPanel.hidden||commandPanel.contains(e.target)||e.target.closest('#btn-composer-menu'))return;
    closeCommands(false);
  },true);
  document.addEventListener('keydown',function(e){
    if(commandPanel.hidden||e.isComposing)return;
    if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();closeCommands(true);return;}
    if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)&&commandPanel.contains(document.activeElement)){
      var buttons=Array.from(commandPanel.querySelectorAll('.composer-command'));if(!buttons.length)return;
      e.preventDefault();var i=buttons.indexOf(document.activeElement);
      i=e.key==='Home'?0:e.key==='End'?buttons.length-1:Math.max(0,Math.min(buttons.length-1,i+(e.key==='ArrowDown'?1:-1)));
      buttons[i].focus({preventScroll:true});buttons[i].scrollIntoView({block:'nearest'});
    }
  },true);
  window.addEventListener('hashchange',()=>{if(location.hash!=='#/chat'&&location.hash!=='#chat'&&location.hash!=='')closeCommands(false);});
  window.addEventListener('resize',positionCommands);window.visualViewport?.addEventListener('resize',positionCommands);window.visualViewport?.addEventListener('scroll',positionCommands);
  if(window.ResizeObserver)new ResizeObserver(positionCommands).observe(document.getElementById('composer'));
  ['btn-composer-menu'].forEach(id=>document.getElementById(id)?.addEventListener('click',function(e){e.preventDefault();e.stopImmediatePropagation();openCommands().catch(err=>toast('菜单打开失败：'+err.message,true));},true));
  ['wb-logout','btn-logout'].forEach(id=>document.getElementById(id)?.addEventListener('click',function(e){e.preventDefault();e.stopImmediatePropagation();logoutAction().catch(err=>toast('退出失败：'+err.message,true));},true));
  document.getElementById('wb-dialog-close').addEventListener('click',function(e){if(actionDialog.dataset.chatFallback){e.preventDefault();e.stopImmediatePropagation();closeAction();}},true);
  actionDialog.addEventListener('cancel',function(e){if(actionDialog.dataset.chatFallback){e.preventDefault();e.stopImmediatePropagation();closeAction();}},true);
  document.addEventListener('keydown',function(e){if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openCommands().catch(err=>toast(err.message,true));}});


  document.getElementById("btn-terminal").addEventListener("click", function () {
    var modal = document.getElementById("terminal-modal");
    var iframe = document.getElementById("terminal-iframe");
    if (iframe.dataset.loaded !== "1") {
      iframe.src = "/terminal";
      iframe.dataset.loaded = "1";
    }
    modal.classList.add("open");
  });
  document.getElementById("btn-close-terminal").addEventListener("click", function () {
    document.getElementById("terminal-modal").classList.remove("open");
  });
  window.addEventListener("message", function (event) {
    if (event.origin === location.origin && event.data && event.data.type === "xgent-close-terminal") {
      document.getElementById("terminal-modal").classList.remove("open");
    }
  });

  document.getElementById("btn-settings").addEventListener("click", function () {
    location.hash = "/settings";
    loadSettings().catch(function (err) { toast("读取配置失败：" + err.message); });
  });
  document.getElementById("btn-close-settings").addEventListener("click", function () { settings.classList.remove("open"); });


  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
      e.preventDefault();
      toggleSearch();
      return;
    }
    if (e.key === "Escape") {
      if (lightbox.classList.contains("open")) {
        closeLightbox();
      } else if (searchBar.classList.contains("open")) {
        closeSearch();
      } else if (document.getElementById("terminal-modal").classList.contains("open")) {
        document.getElementById("terminal-modal").classList.remove("open");
      } else if (settings.classList.contains("open")) {
        settings.classList.remove("open");
      }
    }
  });
  window.addEventListener('xgent-conversation-changed',function(event){
    cancelCardFollow();
    var next=event.detail.conversation_id;
    if(next===selectedConversationId)return;
    if(selectedConversationId){
      conversationDrafts[selectedConversationId]=input.value;
      conversationFiles[selectedConversationId]=pendingFiles.slice();
      try{sessionStorage.setItem(draftKey(),input.value);}catch(e){}
    }
    if(uploadQueue)uploadQueue.cancelled=true;
    selectedConversationId=next;
    queueController?.abort();queueController=null;queueVersion++;queueState=null;queueExpanded=false;drawQueue();
    input.value=conversationDrafts[next]||'';
    try{input.value=input.value||sessionStorage.getItem(draftKey(next))||'';}catch(e){}
    pendingFiles=(conversationFiles[next]||[]).slice();renderPendingFiles();
    historyGen++;historyAnchor=null;historyCursor=null;viewingPast=false;newFrames=0;newerAvailable=false;
    uiGeneration=null;uiRevisions={};uiDeleted={};pendingFrames=[];
    Array.from(new Set(Object.values(byMessageId))).forEach(removeEntry);
    byMessageId={};log.replaceChildren();msgCount=0;lastStreamEntry=null;
    updateEmptyState();syncSendState();
    if(!loggedOut)resync(true).catch(function(){});
  });
  window.addEventListener('xgent-conversation-state',function(event){setBusy(!!event.detail.running);});
  window.addEventListener('xgent-conversation-deleted',function(event){
    const cid=event.detail.conversation_id;delete conversationDrafts[cid];try{sessionStorage.removeItem(draftKey(cid));}catch{}
    if(cardScrollIntent?.cid===cid)cancelCardFollow();
  });
  // The compact launcher is anchored at the bottom-left, not another navigation rail.
  const workspaceMenu=document.getElementById('workspace-menu'),workspaceTrigger=document.getElementById('workspace-settings');
  function positionWorkspaceMenu(){
    const box=workspaceTrigger.getBoundingClientRect(),viewport=window.visualViewport;
    const top=viewport?.offsetTop||0,height=viewport?.height||innerHeight;
    workspaceMenu.style.left=Math.max(8,Math.min(box.left,innerWidth-268))+'px';
    workspaceMenu.style.bottom=Math.max(8,innerHeight-box.top+6)+'px';
    workspaceMenu.style.maxHeight=Math.max(100,box.top-top-16)+'px';
  }
  function closeWorkspaceMenu(restore){workspaceMenu.hidden=true;workspaceTrigger.setAttribute('aria-expanded','false');if(restore)workspaceTrigger.focus();}
  workspaceTrigger.addEventListener('click',()=>{if(!workspaceMenu.hidden){closeWorkspaceMenu(true);return;}workspaceMenu.hidden=false;workspaceTrigger.setAttribute('aria-expanded','true');positionWorkspaceMenu();workspaceMenu.querySelector('a,button')?.focus();});
  document.addEventListener('click',event=>{if(!workspaceMenu.hidden&&!workspaceMenu.contains(event.target)&&!workspaceTrigger.contains(event.target))closeWorkspaceMenu(false);});
  workspaceMenu.addEventListener('click',event=>{if(event.target.closest('a,button'))closeWorkspaceMenu(false);});
  workspaceMenu.addEventListener('keydown',event=>{const items=Array.from(workspaceMenu.querySelectorAll('a,button')).filter(node=>!node.hidden&&!node.disabled);let at=items.indexOf(document.activeElement);if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeWorkspaceMenu(true);}else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();at=event.key==='Home'?0:event.key==='End'?items.length-1:(at+(event.key==='ArrowUp'?-1:1)+items.length)%items.length;items[at]?.focus();}});
  window.addEventListener('resize',()=>{if(!workspaceMenu.hidden)positionWorkspaceMenu();});
  window.addEventListener('hashchange',()=>closeWorkspaceMenu(false));
  window.addEventListener('xgent-auth-expired',()=>closeWorkspaceMenu(false));
  window.XGentWorkspaceMenu={close:restore=>closeWorkspaceMenu(!!restore)};
  window.XGentChat = {
    attachmentChanged: frame=>handleFrame({...frame,type:'attachment_changed'}),
    openCommands:openCommands,logout:logoutAction,
    commandError: function(){
      if(conversationBusy||uploadQueue)return '当前正在生成或上传，请先停止或等待完成后再执行命令。';
      if(!historyReady||currentConnState)return '连接或历史同步尚未完成，请稍后再试。';
      if(pendingFiles.length)return '请先发送或移除待上传附件，再执行命令。';
      return '';
    },
    command: function(command) {
      const error=window.XGentChat.commandError();if(error){toast(error,true);return false;}
      input.value=command;syncSendState();send();return true;
    },
    reload: function(){historyAnchor=null;viewingPast=false;newFrames=0;return resync();},
    locate: async function(key){if(document.body.dataset.page!=='chat')location.hash='/chat';historyAnchor=key;const cid=selectedConversationId;await resync(true);const entry=cid===selectedConversationId&&key&&byMessageId[key];if(entry){entry.row.classList.add('source-target');entry.row.scrollIntoView({block:'center'});setTimeout(()=>entry.row.classList.remove('source-target'),3500);}},
    settings: function(){return loadSettings();},
    commands: function(commands){
      if (!commands.length) return;
      COMMAND_GROUPS=[{group:"全部命令",items:commands}]; FLAT_COMMANDS=commands.slice();
    },
    terminal: function(){document.getElementById("btn-terminal").click();},
    render: function(text,mode,webDisplay){return renderBubbleBody('ai',text,mode,webDisplay);},
    enhance: enhanceBubbleContent
  };
  try { input.value=sessionStorage.getItem(draftKey())||""; syncSendState(); } catch(e) {}
  input.addEventListener("input",function(){try{sessionStorage.setItem(draftKey(),input.value);}catch(e){}});
  window.addEventListener('xgent-auth-expired',function(){
    closeCommands(false);
    if(actionRequest){actionRequest.abort();actionRequest=null;}
    if(actionDialog.open)closeAction();
    loggedOut=true;stopLiveTransport();cancelHistoryPage();historyLoadController?.abort();historyGen++;historyReady=false;pendingFrames=[];
    queueController?.abort();queueController=null;queueVersion++;queueState=null;queueEnabled=false;queueActions.clear();recoveredEdits.clear();textSubmission=null;drawQueue();
    try{sessionStorage.removeItem('xgent_queue_edits');sessionStorage.removeItem('xgent_queue_recovered');}catch(e){}
    cancelUploads('已退出登录');clearPendingFiles();input.value='';try { sessionStorage.removeItem(draftKey()); } catch(e) {}
    Array.from(new Set(Object.values(byMessageId))).forEach(removeEntry);
    log.replaceChildren();byMessageId={};uiRevisions={};uiDeleted={};uiGeneration=null;streamEpoch=null;streamCursor=null;
    hideSuggest();setBusy(false);document.getElementById('terminal-iframe').src='about:blank';
    delete document.getElementById('terminal-iframe').dataset.loaded;
    document.getElementById('terminal-modal').classList.remove('open');
    if(!loginDlg.open)loginDlg.showModal();
  });
  window.dispatchEvent(new CustomEvent("xgent-chat-ready"));
})();
