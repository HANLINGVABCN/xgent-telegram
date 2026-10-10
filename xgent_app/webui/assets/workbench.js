import './appearance.js';
import {updateComposerControls} from './composer-controls.js';
import {PageCache} from './page-cache.js';
import {$,el,button,fmt,stamp,bytes,message,empty,field,select,check,table,formData,api,wb,download,query,tag,copyText,requestId,safeLocalUrl,inlineState} from './components.js';
const pages=[['chat','对话','M4 4h16v12H8l-4 4V4'],['tasks','任务','M8 5h12M8 12h12M8 19h12M3 5h1M3 12h1M3 19h1'],['files','文件与输出','M3 5h7l2 3h9v12H3V5'],['models','模型与技能','M12 3l9 5v8l-9 5-9-5V8l9-5M3 8l9 5 9-5M12 13v8'],['usage','用量','M4 20V10M10 20V4M16 20v-8M22 20H2'],['settings','设置','M4 6h16M4 12h16M4 18h16M9 3v6M16 9v6M8 15v6']];
const state={routed:false,authEpoch:0,route:'chat',boot:null,request:null,detailRequest:null,filters:{},scroll:{},dirty:false,authenticated:false,dialogReturn:null};
const root=$('wb-page');
const pageCache=new PageCache(12);
state.pageKey=null;
state.renderSequence=0;
state.pendingKey=null;
state.retryAfter=0;
function pageKey(){return pageCache.key(state.route,state.filters[state.route]||{});}
function rememberPage(){
  const entry=pageCache.get(state.pageKey);
  if(entry && entry.content.parentNode===root){entry.scroll=root.scrollTop;state.scroll[entry.route]=root.scrollTop;}
}
function pageInteracting(){
  const focused=root.contains(document.activeElement) && document.activeElement.matches('input,textarea,select,button');
  const selection=window.getSelection();
  return state.dirty||state.submitting||$('wb-dialog').open||!$('wb-detail').hidden||focused||
    !!root.querySelector('input[type=checkbox]:checked:not([name])')||
    !!(selection&&!selection.isCollapsed&&root.contains(selection.anchorNode));
}
root.addEventListener('input',()=>{const entry=pageCache.get(state.pageKey);if(entry)entry.interaction++;});
root.addEventListener('change',()=>{const entry=pageCache.get(state.pageKey);if(entry)entry.interaction++;});
function cacheNotice(text,retry=false){
  root.querySelector(':scope > .wb-cache-notice')?.remove();
  if(!text)return;
  const notice=el('div',{class:'wb-cache-notice',role:'status'},el('span',{},text));
  if(retry)notice.append(button('重试更新',()=>render()));
  root.append(notice);
}
window.addEventListener('xgent-workbench-change',event=>{
  pageCache.invalidate(event.detail.pages);state.retryAfter=0;
});
window.addEventListener('xgent-turn-finished',()=>{
  pageCache.invalidate(['tasks','files','usage']);state.retryAfter=0;
  if(['tasks','files','usage'].includes(state.route))backgroundRefresh();
});
function icon(path){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d',path);p.setAttribute('stroke-linejoin','round');p.setAttribute('stroke-linecap','round');svg.append(p);return svg;}
for(const container of [$('wb-nav-links')])for(const [id,title,path] of pages.filter(p=>p[0]!=='settings'))container.append(el('a',{href:'#/'+id,class:'wb-nav-item',role:'menuitem','data-route':id,title},icon(path),el('span',{},title)));
const routeName=()=>location.hash.replace(/^#\/?/,'').split('?')[0]||'chat';
function heading(title,desc,actions=[]){return el('div',{class:'wb-page-head'},el('div',{},el('h1',{},title),el('p',{},desc)),el('div',{class:'wb-actions'},actions));}
function card(title,...children){return el('section',{class:'wb-card'},el('h3',{},title),children);}
function toolbar(...children){return el('div',{class:'wb-toolbar'},children);}
function actions(...children){return el('div',{class:'wb-actions'},children);}
function filter(name,value){
  const route=state.route;state.filters[route]??={};
  if(value!==undefined && state.filters[route][name]!==value){
    // Native controls have already changed their DOM value: that DOM no longer
    // represents the old filter key and must not be restored under it later.
    rememberPage();pageCache.delete(state.pageKey);
    state.filters[route][name]=value;
  }
  return state.filters[route][name];
}
function selectFilter(name,options,defaultValue,fn=render){const input=el('select',{'data-filter':name,'aria-label':({kind:'文件来源',media_type:'文件类型',status:'任务状态',model:'模型筛选',days:'时间范围',trigger:'触发方式'})[name]||name},options.map(o=>el('option',{value:o.value??o},o.label??o)));input.value=filter(name)??defaultValue;input.onchange=()=>{filter(name,input.value);fn();};return input;}
function searchFilter(placeholder,fn=render){const active=state.route;const input=el('input',{type:'search',placeholder,'aria-label':placeholder});input.value=filter('q')||'';let timer;input.oninput=()=>{clearTimeout(timer);filter('q',input.value);timer=setTimeout(()=>{if(state.route!==active||!input.isConnected)return;fn();},300);};return input;}
function showError(node,err){node.querySelector(':scope > .wb-error')?.remove();if(err.name==='AbortError')return;node.append(message(err.message));}
function dirtyForm(form){form.dataset.dirtyTracked="1";form.addEventListener('input',()=>state.dirty=true);form.addEventListener('change',()=>state.dirty=true);}
async function submit(form,fn){
  if(form.dataset.busy)return;
  const values=formData(form),controls=[...form.querySelectorAll('button,input,textarea,select')].map(node=>[node,node.disabled]);
  form.dataset.busy='1';form.setAttribute('aria-busy','true');state.submitting=true;form.querySelector('.wb-error')?.remove();
  controls.forEach(([node])=>node.disabled=true);
  try{await fn(values);state.dirty=false;}catch(e){showError(form,e);}
  finally{state.submitting=false;delete form.dataset.busy;form.removeAttribute('aria-busy');controls.forEach(([node,disabled])=>node.disabled=disabled);}
}
function closeDialog(){if(state.submitting&&state.dirty)return;if(state.dirty&&!confirm('尚有未保存的修改，确定放弃？'))return;state.dirty=false;$('wb-dialog').close();$('wb-dialog-body').replaceChildren();state.dialogReturn?.focus();}
function dialog(title,content){state.dialogReturn=document.activeElement;$('wb-dialog-title').textContent=title;$('wb-dialog-body').replaceChildren(content);$('wb-dialog').showModal();requestAnimationFrame(()=>content.querySelector('input,select,textarea,button')?.focus());}
$('wb-dialog-close').onclick=closeDialog;$('wb-dialog').addEventListener('cancel',e=>{e.preventDefault();closeDialog();});
async function confirmAction(title,explanation,fn,phrase,after){const form=el('form',{},el('p',{class:'wb-note'},explanation));if(phrase)form.append(field(`输入「${phrase}」以确认`,'phrase'));form.append(actions(button('取消',closeDialog),el('button',{class:'wb-btn danger',type:'submit'},'确认操作')));form.onsubmit=e=>{e.preventDefault();submit(form,async data=>{if(phrase&&data.phrase!==phrase)throw new Error('确认文字不匹配');await fn();state.dirty=false;closeDialog();if(after)await after();else await render();});};dialog(title,form);}
function detail(title,content){if(!$('wb-detail').hidden&&!closeDetail())return false;state.detailReturn=document.activeElement;state.detailRequest?.abort();state.detailRequest=new AbortController();state.detailDirty=false;state.detailDocument=null;$('wb-detail-title').textContent=title;$('wb-detail-body').replaceChildren(content);$('wb-detail').hidden=false;$('wb-detail').setAttribute('role',innerWidth<1200?'dialog':'complementary');$('wb-detail').setAttribute('aria-modal',String(innerWidth<1200));if(innerWidth<1200)$('wb-shell').inert=true;document.body.classList.add('wb-detail-open');$('wb-detail-close').focus();return true;}
function closeDetail(force=false){if(!force&&state.detailSaving)return false;if(!force&&state.detailDirty&&!confirm('文档有未保存修改，确定放弃？'))return false;state.detailDirty=false;state.detailDocument=null;state.detailRequest?.abort();$('wb-detail').hidden=true;$('wb-shell').inert=false;document.body.classList.remove('wb-detail-open');state.detailReturn?.focus();return true;}
$('wb-detail-close').onclick=()=>closeDetail();
function errorOr(node,fn){return async()=>{try{await fn();}catch(e){showError(node,e);}};}
async function outputViewer(path,title='完整输出',onBack,source){let current=0,previous=[];const area=el('div');detail(title,area);const signal=state.detailRequest.signal;async function load(offset,back){area.replaceChildren(message('正在读取完整输出…','loading'));try{const page=await api('/api/output/page',{data:{path,offset},signal});current=offset;previous=back;area.replaceChildren(el('p',{class:'wb-note'},`${page.filename} · ${bytes(page.offset)} — ${bytes(page.next_offset)} / ${bytes(page.size)}`),actions(button('上一页',()=>load(previous.at(-1),previous.slice(0,-1))),button('下一页',()=>load(page.next_offset,[...previous,current])),button('复制本页',()=>copyText(page.text).catch(e=>showError(area,e)))),el('pre',{class:'wb-pre wb-output-text'},page.text));area.querySelectorAll('button')[0].disabled=!previous.length;area.querySelectorAll('button')[1].disabled=page.eof;if(source)area.prepend(sourceLink(source));area.prepend(button('收起完整输出',()=>closeDetail()));if(onBack)area.prepend(button('返回任务详情',onBack));}catch(e){area.replaceChildren(message(e.message),button('重试读取',()=>load(offset,back)),onBack?button('返回任务详情',onBack):null);}}await load(0,[]);}
async function refreshBootstrap(){const epoch=state.authEpoch;const boot=await wb('bootstrap');if(epoch!==state.authEpoch)throw new DOMException('登录状态已改变','AbortError');state.boot=boot;window.XGentAppearance?.accept(boot.settings);window.XGentChat?.commands(state.boot.commands||[]);chatControls();return state.boot;}
function chatControls(){if(!state.boot)return;window.XGentConversations?.accept(state.boot.conversations);updateComposerControls(state.boot.settings,changeQuick);}
async function changeQuick(key,value){await wb('settings',{data:{key,value}});await refreshBootstrap();}
function logout(){return window.XGentChat?.logout();}
async function searchMessages(){
  const input=el('input',{type:'search',placeholder:'搜索当前会话历史…','aria-label':'搜索当前会话历史'});
  const results=el('div');const box=el('div',{},el('label',{class:'wb-field'},input),results);
  let controller,timer;
  async function run(before){controller?.abort();controller=new AbortController();results.replaceChildren(message('搜索中…','loading'));try{
    const data=await wb('search'+query({q:input.value,before}),{signal:controller.signal});
    results.replaceChildren(...data.messages.slice().reverse().map(m=>{const text=String(m.content||'').replace(/<[^>]*>/g,'').slice(0,180);
      return el('button',{class:'wb-command-result',onclick:()=>{closeDialog();window.XGentChat?.locate(m.history_key||('history:'+m.id));}},el('span',{},stamp(m.timestamp)),el('strong',{},text));}));
    if(!data.messages.length)results.append(empty('未找到消息','换一个关键词试试。'));
    if(data.next_cursor)results.append(button('更多结果',()=>run(data.next_cursor)));
  }catch(e){if(e.name!=='AbortError')results.replaceChildren(message(e.message));}}
  input.oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>run(),250);};dialog('搜索当前会话历史',box);
}
$('btn-search').addEventListener('click',e=>{e.stopImmediatePropagation();searchMessages();},true);
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='f'){e.preventDefault();e.stopImmediatePropagation();searchMessages();}},true);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('wb-dialog').open)closeDetail();});
// Protect form drafts only; ordinary chat drafts survive route changes automatically.
window.addEventListener('beforeunload',e=>{if(state.dirty||state.detailDirty){e.preventDefault();e.returnValue='';}});
let sourceVisit='',sourceNavigationSequence=0;
function clearSourceReader(){
  $('source-reader')?.remove();document.body.classList.remove('source-readonly');$('log').hidden=false;
  $('source-navigation').hidden=true;$('source-reader-title')?.remove();
}
async function followSource(){
  const params=new URLSearchParams(location.hash.split('?')[1]||''),cid=params.get('conversation'),anchor=params.get('message');
  if(!cid){clearSourceReader();sourceVisit='';return;}
  const signature=cid+'|'+(anchor||'');if(sourceVisit===signature)return;sourceVisit=signature;
  const sequence=++sourceNavigationSequence,stillHere=()=>sequence===sourceNavigationSequence&&state.route==='chat'&&sourceVisit===signature;
  const banner=$('source-navigation');banner.hidden=false;banner.replaceChildren(el('span',{},'正在定位来源消息…'));
  try{
    await window.XGentConversations.refresh();if(!stillHere())return;
    const owner=window.XGentConversations.items.find(c=>c.id===cid);if(!owner)throw new Error('来源对话已不存在');
    const back=el('a',{href:'#/'+(state.sourceReturn||'tasks')},'返回'+(pages.find(p=>p[0]===state.sourceReturn)?.[1]||'工作台'));
    if(owner.archived){
      const result=await wb('history'+query({conversation_id:cid,anchor,limit:50}));if(!stillHere())return;
      clearSourceReader();document.body.classList.add('source-readonly');$('log').hidden=true;
      const name=el('span',{id:'source-reader-title'},owner.name+' · 已归档');$('header').querySelector('.header-text').append(name);
      const reader=el('section',{id:'source-reader','aria-label':'来源对话（只读）'}),messages=el('div');reader.append(messages);$('wb-chat').append(reader);
      function appendMessages(records,prepend=false){const fragment=document.createDocumentFragment();for(const record of records){const role=record.role==='user'?'user':record.msg_type==='agent_result'?'cmd':record.msg_type==='token_usage'?'token':record.role==='assistant'?'ai':'sys';const body=el('div',{class:'body'});if(role==='user'){body.textContent=record.content;body.style.whiteSpace='pre-wrap';}else{body.innerHTML=window.XGentChat.render(record.content,record.parse_mode);window.XGentChat.enhance(body);}const row=el('article',{class:'msg-row '+role,'data-source-message':record.history_key||'history:'+record.id},el('div',{class:'bubble '+role},el('div',{class:'bubble-header'},({user:'你',cmd:'工具结果',token:'Token 用量',ai:'XGent',sys:'系统'})[role],el('span',{class:'ts'},stamp(record.timestamp))),body));for(const media of record.media||[]){if(safeLocalUrl(media.download_url))body.append(el('a',{href:media.download_url,download:media.filename},media.filename));}fragment.append(row);}if(prepend)messages.prepend(fragment);else messages.append(fragment);}
      appendMessages(result.messages||[]);
      let before=result.first_cursor||result.next_cursor,after=result.last_cursor;
      const earlier=button('加载更早消息',async()=>{earlier.disabled=true;try{const older=await wb('history'+query({conversation_id:cid,before,limit:50}));if(!stillHere())return;const height=reader.scrollHeight;appendMessages(older.messages||[],true);reader.scrollTop+=reader.scrollHeight-height;before=older.first_cursor;earlier.hidden=!older.next_cursor;}catch(e){banner.append(message(e.message));}finally{earlier.disabled=false;}});
      const newer=button('加载较新消息',async()=>{newer.disabled=true;try{const next=await wb('history'+query({conversation_id:cid,after,limit:50}));if(!stillHere())return;appendMessages(next.messages||[]);after=next.last_cursor;newer.hidden=!next.next_cursor;}catch(e){banner.append(message(e.message));}finally{newer.disabled=false;}});
      reader.prepend(actions(earlier));reader.append(actions(newer));earlier.hidden=!result.next_cursor;newer.hidden=!anchor;
      const target=Array.from(messages.children).find(row=>row.dataset.sourceMessage===anchor);if(target){target.classList.add('source-target');requestAnimationFrame(()=>target.scrollIntoView({block:'center'}));}
    }else{
      clearSourceReader();state.navigatingSource=true;
      try{await window.XGentConversations.select(cid);}finally{state.navigatingSource=false;}
      if(!stillHere())return;await window.XGentChat.locate(anchor||null);if(!stillHere())return;
    }
    banner.hidden=false;banner.replaceChildren(back,el('span',{},owner.name+(owner.archived?' · 已归档，只读':'')+(anchor?' · 已定位来源消息':' · 此记录没有对应消息位置')));
  }catch(error){if(stillHere())banner.replaceChildren(message(error.message),el('a',{href:'#/'+(state.sourceReturn||'tasks')},'返回工作台'));}
}
document.addEventListener('click',event=>{const link=event.target.closest('.wb-source-link');if(link&&!event.ctrlKey&&!event.metaKey&&!event.shiftKey&&event.button===0){state.sourceReturn=state.route;sourceVisit='';}});

async function route(){
  const requested=routeName();
  const next=pages.some(p=>p[0]===requested)?requested:'chat';
  if(state.routed&&next===state.route && document.body.dataset.page===next){
    if(state.authenticated&&next!=='chat'&&!pageCache.get(pageKey()))await render({reuse:true});
    if(state.authenticated&&next==='chat')await followSource();
    return;
  }
  if(state.detailSaving){history.replaceState(null,'','#/'+state.route);return;}
  if(state.dirty||state.detailDirty){
    if(!confirm('尚有未保存修改，放弃并切换页面？')){history.replaceState(null,'','#/'+state.route);return;}
    pageCache.delete(state.pageKey); // Do not resurrect a discarded edit as a clean form.
  }
  rememberPage();
  state.request?.abort();state.renderSequence++;state.pendingKey=null;
  state.dirty=false;state.detailDirty=false;
  if(state.route==='settings'&&next!=='settings')window.XGentAppearance?.cancelPreview();
  if(next!=='chat'){sourceNavigationSequence++;sourceVisit='';clearSourceReader();}
  state.route=next;state.retryAfter=0;
  state.routed=true;document.body.dataset.page=next;
  $('wb-page-title').textContent=pages.find(p=>p[0]===next)?.[1]||'';$('wb-page-title').hidden=next==='chat';
  window.XGentWorkspaceMenu?.close();
  document.querySelectorAll('[data-route]').forEach(n=>{n.classList.toggle('active',n.dataset.route===next);n.setAttribute('aria-current',n.dataset.route===next?'page':'false');});
  root.hidden=next==='chat';closeDetail();
  if(state.authenticated&&next!=='chat')await render({reuse:true});
  else root.replaceChildren(); // Chat has its own retained DOM; never cache/reset the transcript here.
  if(next==='chat'&&state.authenticated)await followSource();
}
window.addEventListener('hashchange',route);

async function render(options={}){
  if(state.route==='chat'||!state.authenticated)return;
  const background=options.background===true;
  const passive=background||options.reuse===true;
  const active=state.route,key=pageKey();
  if(background && (document.hidden||pageInteracting()||state.pendingKey===key))return;
  const previousKey=state.pageKey;
  rememberPage();
  let entry=pageCache.get(key);
  if(entry && (options.reuse===true||previousKey!==key)){
    root.replaceChildren(entry.content);root.scrollTop=entry.scroll;state.pageKey=key;document.body.dataset.loadedPage=active;
    if(pageCache.fresh(entry))return;
    if(pageInteracting()){cacheNotice('已有新数据，完成当前操作后更新');return;}
  }
  if(background&&entry&&pageCache.fresh(entry))return;
  if(options.reuse===true&&state.pendingKey===key&&!state.request?.signal.aborted)return;
  const search=root.querySelector('input[type=search]');
  const searchFocused=search===document.activeElement;
  const searchCursor=searchFocused?search.selectionStart:null;
  const previousScroll=entry?.content.parentNode===root?root.scrollTop:(entry?.scroll||0);
  state.request?.abort();
  const controller=new AbortController();state.request=controller;
  const sequence=++state.renderSequence;
  state.pendingKey=key;state.pageKey=key;
  const revision=pageCache.revision(active),interaction=entry?.interaction||0;
  // Initial visits may load; updates leave the usable page in place until data is ready.
  if(!entry && (!root.firstElementChild||document.body.dataset.loadedPage!==active)){root.replaceChildren(message('正在加载工作区…','loading'));root.scrollTop=0;}
  else if(!passive)cacheNotice('正在更新，当前内容仍可查看');
  const assertCurrent=()=>{
    if(controller.signal.aborted||sequence!==state.renderSequence||state.route!==active||pageKey()!==key||!state.authenticated)
      throw new DOMException('Page navigation superseded request','AbortError');
  };
  const dataVersions=[];
  const signature=(name,value)=>{
    // Relative time boundaries aren't content changes. Rows, status, settings and
    // revisions still take part in the signature, including external changes.
    const stable=name.startsWith('usage?')?{...value,start:null,end:null}:value;
    dataVersions.push(JSON.stringify([name.split('?')[0],stable]));
  };
  const read=async name=>{assertCurrent();const value=await wb(name,{signal:controller.signal});assertCurrent();signature(name,value);return value;};
  read.api=async path=>{assertCurrent();const value=await api(path,{signal:controller.signal});assertCurrent();signature(path,value);return value;};
  try{
    const content=await ({tasks:taskPage,files:filePage,models:modelPage,usage:usagePage,settings:settingsPage}[active])(read);
    assertCurrent();
    if(revision!==pageCache.revision(active))return; // A concurrent mutation owns the next refresh.
    const dataVersion=JSON.stringify(dataVersions.sort());
    if(entry && entry.dataVersion===dataVersion){
      entry.updatedAt=Date.now();entry.revision=revision;state.retryAfter=0;cacheNotice('');return;
    }
    if(entry && (state.dirty||entry.interaction!==interaction||passive&&pageInteracting())){
      cacheNotice('已有新数据，完成当前操作后更新');return;
    }
    // Scroll may have moved while a slow request was pending; honor the latest position.
    const scroll=entry?.content.parentNode===root?root.scrollTop:previousScroll;
    entry=pageCache.set(key,active,content,revision,scroll);
    entry.dataVersion=dataVersion;
    root.replaceChildren(content);root.scrollTop=scroll;state.retryAfter=0;document.body.dataset.loadedPage=active;
    if(searchFocused){const input=root.querySelector('input[type=search]');if(input){input.focus({preventScroll:true});if(searchCursor!==null)input.setSelectionRange(searchCursor,searchCursor);}}
  }catch(e){
    if(e.name==='AbortError'||sequence!==state.renderSequence||!state.authenticated||state.route!==active)return;
    state.retryAfter=Date.now()+15000;
    if(entry){cacheNotice('更新失败，当前显示上次加载的内容：'+e.message,true);}
    else root.replaceChildren(heading(pages.find(p=>p[0]===active)?.[1]||'','无法加载此工作区'),message(e.message),button('重新加载',()=>render()));
  }finally{
    if(sequence===state.renderSequence){
      state.pendingKey=null;
      const notice=root.querySelector(':scope > .wb-cache-notice');
      if(notice?.textContent==='正在更新，当前内容仍可查看')notice.remove();
    }
  }
}
function backgroundRefresh(){
  if(!state.authenticated||state.route==='chat'||document.hidden||Date.now()<state.retryAfter||pageInteracting())return;
  render({background:true});
}

const activeTask=t=>!['completed','failed','cancelled'].includes(t.status);
const triggerLabel=t=>t.condition_expr?'条件触发':({cron:'Cron 定时',once:'单次执行',immediate:'立即执行'}[t.schedule_type]||t.schedule_type||'—');
function sourceLink(source){
  if(!source?.conversation_id)return el('span',{class:'wb-source-unknown'},'来源未关联');
  const params=new URLSearchParams({conversation:source.conversation_id});
  if(source.message_key)params.set('message',source.message_key);
  if(source.archived)params.set('archived','1');
  const link=el('a',{href:'#/chat?'+params,class:'wb-source-link',title:source.message_key?'打开来源对话并定位到消息':source.note||'打开来源对话'},source.conversation_name||'新对话',source.archived?el('small',{},'已归档'):null);
  return el('div',{class:'wb-source-cell'},link,!source.message_key?el('small',{},source.note||'无对应消息位置'):null);
}
function filePagination(data){
  const page=data.offset||0,previous=button('上一页',()=>{filter('offset',Math.max(0,page-50));render();}),next=button('下一页',()=>{filter('offset',data.next_offset);render();});previous.disabled=!page;next.disabled=data.next_offset==null;
  return el('div',{class:'wb-pagination'},el('span',{class:'wb-note'},data.total?'共 '+fmt(data.total)+' 项 · '+(page+1)+'–'+(page+data.items.length):'共 0 项'),actions(previous,next));
}

function taskStamp(value,zone){
  if(!value)return '—';
  try{return new Date(value*1000).toLocaleString('zh-CN',{timeZone:zone||'Asia/Shanghai',hour12:false});}
  catch{return stamp(value);}
}
function taskNext(t){
  if(!activeTask(t))return '已结束';
  if(t.status==='running')return '正在执行';
  if(t.status==='waiting_delivery')return '等待 Agent 接收结果';
  return t.next_run_at?taskStamp(t.next_run_at,t.timezone):'等待调度';
}
function cancelTasks(ids,after){
  confirmAction('取消任务',`将取消 ${ids.length} 个任务，停止后续调度并终止正在运行的命令。Telegram、网页和 CLI 共享的任务都会受影响；保留已有运行记录。`,
    ()=>wb('tasks/cancel',{data:{ids,confirm:true}}),null,after);
}
async function taskPage(read){
  const status=filter('status')||'',q=filter('q')||'',kind=filter('trigger')||'',source=filter('source')||'';
  const data=await read('tasks'+query({status,q,kind,source_conversation_id:source,offset:filter('offset')||0}));
  const page=el('div',{class:'wb-task-page'},heading('任务中心','管理 Trigger 的计划、条件与执行结果；所有通道共享同一份任务。',
    [button('创建任务',()=>taskForm(data.timezone),'primary')]));
  const counts=data.counts||{},stats=el('div',{class:'wb-task-stats'});
  for(const [label,value] of [['待执行',(counts.scheduled||0)+(counts.pending||0)],['运行 / 待通知',(counts.running||0)+(counts.waiting_delivery||0)],['已完成',counts.completed||0],['失败',counts.failed||0]])
    stats.append(el('div',{},el('span',{},label),el('strong',{},fmt(value))));
  page.append(stats);
  const selected=new Set(),boxes=[];
  const selection=el('span',{class:'wb-note',role:'status'},'仅可选择未结束的任务');
  const cancel=button('取消选中',()=>cancelTasks([...selected]),'danger');cancel.disabled=true;
  const all=el('input',{type:'checkbox','aria-label':'选择本页全部未结束任务'});
  const updateSelection=()=>{cancel.disabled=!selected.size;all.checked=!!boxes.length&&selected.size===boxes.length;all.indeterminate=selected.size>0&&selected.size<boxes.length;selection.textContent=selected.size?`已选 ${selected.size} 项`:'仅可选择未结束的任务';};
  all.onchange=()=>{for(const [box,id] of boxes){box.checked=all.checked;all.checked?selected.add(id):selected.delete(id);}updateSelection();};
  const change=()=>{filter('offset',0);render();};
  const statusSelect=selectFilter('status',[{value:'',label:'全部状态'},...['scheduled','pending','running','waiting_delivery','completed','failed','cancelled'].map(v=>({value:v,label:tag(v).textContent}))],'',change);
  const kindSelect=selectFilter('trigger',[{value:'',label:'全部触发方式'},{value:'once',label:'单次执行'},{value:'cron',label:'Cron 定时'},{value:'condition',label:'条件触发'},{value:'immediate',label:'立即执行'}],'',change);
  const sourceSelect=selectFilter('source',[{value:'',label:'全部来源对话'},...(data.sources||[]).map(c=>({value:c.id,label:(c.name||'新对话')+' · '+c.id.slice(0,6)+(c.archived?'（已归档）':'')}))],'',change);
   sourceSelect.setAttribute('aria-label','任务来源对话');
   const sourceSearch=button('',()=>taskSourceSearch());sourceSearch.classList.add('wb-source-search');sourceSearch.setAttribute('aria-label','搜索对话记录');sourceSearch.title='搜索对话名称或历史内容';sourceSearch.innerHTML='<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-search"/></svg>';
   page.append(toolbar(searchFilter('搜索任务名称、命令或 ID',change),el('div',{class:'wb-source-picker'},sourceSelect,sourceSearch),statusSelect,kindSelect,button('清除筛选',()=>{for(const key of ['q','source','status','trigger'])filter(key,'');change();}),button('刷新',()=>render())));
  page.append(el('div',{class:'wb-selection-bar'},el('label',{class:'wb-check'},all,'全选本页'),selection,cancel));
  all.disabled=!data.items.some(activeTask);
  if(!data.items.length){
    page.append(status||q||kind||source?empty('没有匹配的任务','试试其他状态、触发方式或关键词。',button('清除筛选',()=>{for(const k of ['status','q','trigger','source'])filter(k,'');filter('offset',0);render();})):
      empty('暂时没有任务','支持延时、指定时间、Cron 和输出条件触发。查看本页不会执行命令。',button('创建第一个任务',()=>taskForm(data.timezone),'primary')));
  }else{
    const labels=['选择','来源对话','任务 / 命令','触发规则','状态','下次执行','执行次数','操作'];
    const list=table(labels,data.items.map(t=>{
      const box=el('input',{type:'checkbox',disabled:!activeTask(t),'aria-label':'选择 '+(t.summary||t.id),onchange:e=>{e.target.checked?selected.add(t.id):selected.delete(t.id);updateSelection();}});
      if(activeTask(t))boxes.push([box,t.id]);
      const title=el('div',{class:'wb-task-title'},el('button',{class:'link',type:'button',onclick:()=>taskDetail(t.id)},t.summary||t.id),el('code',{class:'wb-task-command',title:t.command},t.command));
      const rule=el('div',{},el('strong',{},triggerLabel(t)),el('small',{class:'wb-task-rule',title:t.schedule_expr||''},t.schedule_expr||(t.condition_expr?'命令启动后监控输出':'立即执行，无时间计划')),t.condition_expr?el('small',{class:'wb-task-rule',title:t.condition_expr},'条件：'+t.condition_expr):null,t.repeat?el('small',{},'命中后重复监控'):null);
      return [el('label',{class:'wb-task-select'},box),sourceLink(t.source),title,rule,tag(t.status),el('div',{},taskNext(t),activeTask(t)?el('small',{},t.timezone):null),
        el('div',{},fmt(t.fire_count),t.failure_count?el('small',{class:'wb-danger-text'},`失败 ${fmt(t.failure_count)} 次`):null),
        actions(button('详情',()=>taskDetail(t.id)),activeTask(t)?button('取消',()=>cancelTasks([t.id]),'danger'):null)];
    }));
    list.classList.add('wb-task-table');
    for(const row of list.querySelectorAll('tbody tr'))[...row.children].forEach((cell,i)=>cell.dataset.label=labels[i]);
    page.append(list);
  }
  const offset=data.offset||0;
  const previous=button('上一页',()=>{filter('offset',Math.max(0,offset-50));render();});previous.disabled=!offset;
  const next=button('下一页',()=>{filter('offset',data.next_offset);render();});next.disabled=data.next_offset===null;
  page.append(el('div',{class:'wb-pagination'},el('span',{class:'wb-note'},data.total?`共 ${fmt(data.total)} 项 · 当前 ${offset+1}–${offset+data.items.length}`:'共 0 项'),actions(previous,next)));
  return page;
}
function taskForm(timezone) {
  const zone=timezone||'Asia/Shanghai',drafts={};let currentMode;
  const target=window.XGentConversations?.current;
  const targetName=document.getElementById('wb-conversation-title')?.textContent||target;
  const form=el('form',{},el('p',{class:'wb-note'},'任务来源：'+targetName+' · '+String(target||'').slice(0,6)),field('任务名称','task','','text','描述任务目的，便于查看执行记录'),
    field('执行命令','command','','textarea','使用服务运行账号执行；不会提供沙箱隔离。'),
    select('触发方式','mode',[{value:'after',label:'延时执行'},{value:'at',label:'指定时间'},{value:'cron',label:'Cron 定时'},{value:'when',label:'输出条件触发'}],'after'));
  const schedule=el('div'),summary=el('div',{class:'wb-callout','aria-live':'polite'});
  const configuration={after:{label:'等待时间',value:'30s',hint:'支持 30s、5m、2h、1d 等格式。',presets:[['30 秒','30s'],['5 分钟','5m'],['1 小时','1h']]},
    at:{label:'执行时间',type:'datetime-local',hint:'按指定时区解释，不使用浏览器本地时区。'},
    cron:{label:'Cron 表达式',value:'0 9 * * *',hint:'五个字段：分 时 日 月 周；周建议用 mon–sun（数字 0 为周一）。',presets:[['每小时','0 * * * *'],['每天 09:00','0 9 * * *'],['工作日 09:00','0 9 * * mon-fri']]},
    when:{label:'触发条件',value:'READY',hint:'命令启动后持续监控输出；匹配条件时通知 Agent。'}};
  function updateSummary(){
    const mode=form.elements.mode.value,value=form.elements.expression?.value||'未填写';
    summary.replaceChildren(el('strong',{},'创建后会发生什么'),el('p',{},mode==='when'?'立即启动命令并等待输出匹配。':'交由服务器调度器按计划执行。'),
      el('p',{},`${configuration[mode].label}：${value} · 时区：${form.elements.timezone.value||zone}`),
      el('small',{},'仅确认创建才会登记任务。查看预览和切换触发方式不会执行命令。'));
  }
  function scheduleForm(){
    if(currentMode)drafts[currentMode]={expression:form.elements.expression.value,repeat:!!form.elements.repeat?.checked};
    const mode=form.elements.mode.value,c=configuration[mode],draft=drafts[mode];currentMode=mode;
    schedule.replaceChildren(field(c.label,'expression',draft?.expression??c.value??'',c.type||'text',c.hint));
    form.elements.expression.required=true;
    if(c.presets)schedule.append(el('div',{class:'wb-presets'},c.presets.map(([label,value])=>button(label,()=>{form.elements.expression.value=value;form.elements.expression.dispatchEvent(new Event('input',{bubbles:true}));}))));
    if(mode==='when')schedule.append(check('条件命中后重复监控','repeat',draft?.repeat));
    updateSummary();
  }
  form.elements.mode.onchange=scheduleForm;
  form.append(schedule,field('时区','timezone',zone,'text','所有计划按此时区执行，例如 Asia/Shanghai。'),summary);scheduleForm();
  let id=requestId();form.addEventListener('input',()=>{id=requestId();updateSummary();});form.addEventListener('change',()=>{id=requestId();updateSummary();});
  form.elements.task.required=true;form.elements.command.required=true;form.elements.timezone.required=true;
  form.append(actions(button('取消',closeDialog),el('button',{class:'wb-btn primary',type:'submit'},'确认创建任务')));dirtyForm(form);
  form.onsubmit=e=>{e.preventDefault();submit(form,async d=>{
    if(!d.task.trim()||!d.command.trim())throw new Error('请填写任务名称和执行命令');
    if(!d.expression)throw new Error('请填写触发时间或条件');
    const expression=d.mode==='at'?d.expression.replace('T',' '):d.expression;
    await wb('tasks/create',{data:{conversation_id:target,request_id:id,task:d.task,command:d.command,timezone:d.timezone,
      [d.mode]:expression,...(d.mode==='when'?{repeat:!!d.repeat}:{})}});
    state.dirty=false;closeDialog();for(const key of ['status','q','trigger','source'])filter(key,'');filter('offset',0);await render();
  });};dialog('创建后台任务',form);
}

async function taskDetail(id){
  const box=el('div',{},message('读取任务详情…','loading'));detail('任务详情',box);
  const signal=state.detailRequest.signal;
  try{
    const data=await wb('tasks'+query({id}),{signal}),t=data.task;
    const showRun=r=>card(taskStamp(r.started_at||r.created_at,t.timezone),sourceLink(r.source),tag(r.status),el('p',{class:'wb-note'},'退出码 '+(r.exit_code??'—')),
      r.error?message(r.error):null,r.output_path?button('查看完整输出',()=>outputViewer(r.output_path,'任务输出',()=>taskDetail(id))):el('pre',{class:'wb-pre'},r.output||'暂无输出'));
    box.replaceChildren(el('h2',{class:'wb-detail-filename'},t.summary||id),actions(tag(t.status),button('刷新状态',()=>taskDetail(id))),
      el('dl',{},[['任务 ID',t.id],['来源对话',sourceLink(t.source)],['触发方式',triggerLabel(t)],['计划',t.schedule_expr],['输出条件',t.condition_expr],['重复监控',t.repeat?'是':'否'],['时区',t.timezone],
        ['下次执行',taskNext(t)],['创建时间',taskStamp(t.created_at,t.timezone)],['执行 / 失败',`${fmt(t.fire_count)} / ${fmt(t.failure_count||0)}`]]
        .filter(([k,v])=>v!=null&&v!=='').map(([k,v])=>el('div',{class:'wb-detail-row'},el('dt',{},k),el('dd',{},v)))),
      el('h3',{class:'wb-section-title'},'执行命令'),el('pre',{class:'wb-pre wb-output-text'},t.command),button('复制命令',errorOr(box,async()=>{await copyText(t.command);inlineState(box,'命令已复制');})));
    if(t.last_error)box.append(message(t.last_error));
    if(activeTask(t))box.append(button('取消任务',()=>cancelTasks([id],async()=>{await render();if(!signal.aborted)await taskDetail(id);}),'danger'));
    const runs=el('div',{class:'wb-run-list'});
    box.append(el('h3',{class:'wb-section-title'},'运行记录'),runs);
    for(const r of data.runs)runs.append(showRun(r));
    if(!data.runs.length)runs.append(empty('还没有运行记录','任务执行后，结果和输出会保存在这里。'));
    if(data.next_cursor){
      let cursor=data.next_cursor;
      const more=button('更早运行记录',async()=>{more.disabled=true;try{
        const older=await wb('tasks'+query({id,before:cursor}),{signal});
        for(const r of older.runs)runs.append(showRun(r));
        cursor=older.next_cursor;if(!cursor)more.remove();
      }catch(e){showError(box,e);}finally{more.disabled=false;}});box.append(more);
    }
  }catch(e){if(e.name!=='AbortError')box.replaceChildren(message(e.message),button('重试读取',()=>taskDetail(id)));}
}


async function filePage(read){
  const kind=filter('kind')||'files',source=filter('source')||'all';
  const data=await read('artifacts'+query({kind,source_conversation_id:source,q:filter('q'),media_type:filter('media_type'),availability:filter('availability'),execution_status:filter('execution_status'),start_date:filter('start_date'),end_date:filter('end_date'),sort:filter('sort')||'newest',offset:filter('offset')||0}));
  const page=el('div',{class:'wb-file-page'},heading('文件与输出','按来源对话查找附件、生成文件和完整命令日志。'));
  const change=()=>{filter('offset',0);render();};
  const tabs=el('div',{class:'wb-tabs',role:'tablist','aria-label':'文件来源'},[['files','附件与生成文件'],['outputs','命令输出']].map(([value,label])=>el('button',{type:'button',role:'tab','aria-selected':kind===value,class:kind===value?'active':'',onclick:()=>{filter('kind',value);filter('media_type','');filter('execution_status','');change();}},label)));
  const origin=selectFilter('source',[{value:'all',label:'全部来源对话'},{value:'current',label:'当前对话'},...(data.sources||[]).map(c=>({value:c.id,label:c.name+(c.archived?'（已归档）':'')}))],source,change);origin.setAttribute('aria-label','文件来源对话');
  const origins=el('div',{class:'wb-source-picker'},origin,button('搜索对话',taskSourceSearch));
  const clear=()=>{for(const key of ['q','media_type','availability','execution_status','start_date','end_date'])filter(key,'');filter('source','all');filter('sort','newest');change();};
  const tools=toolbar(searchFilter(kind==='outputs'?'搜索日志、命令或任务':'按文件名搜索',change),origins,
    kind==='files'?selectFilter('media_type',[{value:'',label:'全部文件类型'},{value:'photo',label:'图片'},{value:'video',label:'视频'},{value:'audio',label:'音频'},{value:'file',label:'文档'}],'',change):selectFilter('execution_status',[{value:'',label:'全部执行状态'},...['completed','failed','interrupted','running'].map(value=>({value,label:tag(value).textContent}))],'',change),
    selectFilter('availability',[{value:'',label:'全部文件状态'},{value:'available',label:'可读取'},{value:'missing',label:'不可读取'}],'',change),selectFilter('sort',[{value:'newest',label:'时间：从新到旧'},{value:'oldest',label:'时间：从旧到新'},{value:'name',label:'名称'},{value:'size',label:'大小：从大到小'}],'newest',change));
  const dates=el('form',{class:'wb-file-dates'},field('开始日期','start_date',filter('start_date')||'','date'),field('结束日期','end_date',filter('end_date')||'','date'),el('button',{type:'submit',class:'wb-btn'},'应用时间'),button('清除筛选',clear),button('刷新',()=>render()));
  dates.onsubmit=e=>{e.preventDefault();const values=formData(dates);filter('start_date',values.start_date);filter('end_date',values.end_date);change();};page.append(tabs,tools,dates);
  if(!data.items.length)page.append(empty('没有匹配的文件','可调整来源、类型或时间范围。',button('清除筛选',clear)));
  else {
    const labels=['来源对话',kind==='outputs'?'命令 / 日志':'文件','类型 / 大小','时间','操作'];
    const tableNode=table(labels,data.items.map(file=>[sourceLink(file.source),el('div',{class:'wb-file-title'},el('button',{class:'link',type:'button',onclick:()=>fileDetail(file)},file.filename||'文件'),file.command?el('code',{class:'wb-file-command',title:file.command},file.command):null,file.task_name?el('small',{},file.task_name):null,file.error?el('small',{class:'wb-danger-text'},file.error):null),el('div',{},({photo:'图片',video:'视频',audio:'音频',output:'命令输出',file:'文档'})[file.kind]||'文件',el('small',{},bytes(file.size)),file.status?tag(file.status):null),stamp(file.timestamp),button(file.kind==='output'?'查看完整输出':'预览',()=>fileDetail(file))]));
    tableNode.classList.add('wb-file-table');for(const row of tableNode.querySelectorAll('tbody tr'))[...row.children].forEach((cell,index)=>cell.dataset.label=labels[index]);page.append(tableNode);
  }
  page.append(filePagination(data));return page;
}
async function fileDetail(file) {
  if(file.kind==='output'){await outputViewer(file.path,file.filename,null,file.source);return;}
  const box=el('div',{},el('h2',{class:'wb-detail-filename'},file.filename||'文件'));
  const url=file.download_url;
  detail('文件详情',box);const signal=state.detailRequest.signal;
  const metadata=el('dl',{class:'wb-file-facts'},...Object.entries({'类型':file.mime_type||file.kind||'未知','大小':bytes(file.size),'时间':stamp(file.timestamp)}).map(([k,v])=>el('div',{class:'wb-detail-row'},el('dt',{},k),el('dd',{},v))));
  box.append(sourceLink(file.source),metadata);
  if(file.error){box.append(message(file.error));return;}
  if(!safeLocalUrl(url)){box.append(message('文件地址无效或已缺失，请重新加载记录。'));return;}
  const tools=actions(el('a',{href:url,download:file.filename,class:'wb-btn primary'},'下载原文件'));
  if(file.source?.conversation_id){const jump=sourceLink(file.source);jump.classList.add('wb-file-source-action');tools.append(jump);}
  box.append(tools,el('div',{class:'wb-spacer'}));
  if(file.kind==='photo'&&file.mime_type!=='image/svg+xml')box.append(el('img',{src:url,alt:file.filename,loading:'lazy',onerror:()=>box.append(message('图片无法读取，文件可能已移动。'))}));
  else if(['video','audio'].includes(file.kind))box.append(el(file.kind,{src:url,controls:'',preload:'metadata'}));
  else {
    const preview=el('div');box.append(preview);let previous=[];
    async function load(offset,back){preview.replaceChildren(message('读取文件预览…','loading'));try{
      const page=await wb('artifacts/preview'+query({id:file.id,offset,conversation_id:file.source?.conversation_id}),{signal});previous=back;
      const pre=el('pre',{class:'wb-pre wb-document-preview',tabindex:'0','aria-label':'文件文本预览'},page.text);
      const prev=button('上一页',()=>load(previous.at(-1),previous.slice(0,-1)));
      const next=button('下一页',()=>load(page.next_offset,[...previous,offset]));prev.disabled=!back.length;next.disabled=page.eof;
      preview.replaceChildren(el('p',{class:'wb-note'},`${page.encoding} · ${bytes(offset)}–${bytes(page.next_offset)} / ${bytes(page.size)}`),
        actions(prev,next,button('复制本页',async()=>{try{await copyText(page.text);inlineState(preview,'本页已复制');}catch(e){showError(preview,e);}}),button('切换换行',()=>pre.classList.toggle('nowrap'))),pre);
    }catch(e){if(e.name==='AbortError')return;preview.replaceChildren(e.status===415?empty('原文件可下载',e.message):message(e.message));}}
    await load(0,[]);
  }
}

async function modelPage(read){const mode=filter('mode')||'providers';const page=el('div',{},heading('模型与技能','连接你的模型，让 Agent 具备所需的能力。',mode==='providers'?[button('导入 / 导出',importExport),button('添加提供商',()=>providerForm(null),'primary')]:[]));const tabs=el('div',{class:'wb-tabs'},button('模型提供商',()=>{filter('mode','providers');render();},mode==='providers'?'active':''),button('技能库',()=>{filter('mode','skills');render();},mode==='skills'?'active':''),button('记忆',()=>{filter('mode','memories');render();},mode==='memories'?'active':''));page.append(tabs);
 if(mode==='skills'||mode==='memories'){const {knowledgePage}=await managementModule('knowledge');await knowledgePage(page,await read(mode),mode,{state,filter,localFilters,render,detail,closeDetail,confirmAction,refreshBootstrap,toolbar});return page;}
 const [boot,data]=await Promise.all([read('bootstrap'),read('providers')]);state.boot=boot;chatControls();state.providerFormats=data.formats;const providerSearch=el('input',{type:'search','aria-label':'搜索提供商或模型',placeholder:'搜索提供商名称、地址或模型…'}),providerCounts=el('span',{class:'wb-note',role:'status'});providerSearch.value=filter('providerQuery')||'';const grid=el('div',{class:'wb-grid wb-provider-list'});page.append(toolbar(providerSearch,providerCounts));for(const p of data.items){const c=card(p.name,el('p',{},p.base_url),actions(tag(p.api_format),tag(p.has_key?'已配置密钥':'未配置密钥'),state.boot.settings.values.chat_model?.startsWith(p.name+'|')?el('span',{class:'wb-tag good'},'当前对话'):null,state.boot.media_model?.provider===p.name?el('span',{class:'wb-tag good'},'当前媒体'):null),el('p',{},`${p.models.length} 个已保存模型`));const current=state.boot.settings.values.chat_model?.split('|');const choices=el('select',{'aria-label':p.name+' 模型'},p.models.map(m=>el('option',{value:m,selected:current?.[0]===p.name&&current?.[1]===m},m)));c.append(el('label',{class:'wb-field'},el('span',{},'选择模型'),choices),actions(button('用于对话',errorOr(c,async()=>{await wb('providers/select',{data:{target:'chat',provider:p.name,model:choices.value}});await refreshBootstrap();inlineState(c,'已设置为对话模型');render();})),button('用于媒体',errorOr(c,async()=>{await wb('providers/select',{data:{target:'media',provider:p.name,model:choices.value}});await refreshBootstrap();inlineState(c,'已设置为媒体模型');render();}))),el('div',{class:'wb-card-footer'},button('编辑配置',()=>providerForm(p)),button('删除',()=>confirmAction('删除提供商','删除后，依赖此提供商的默认模型需要重新选择。',()=>wb('providers/delete',{data:{name:p.name,confirm:true}})),'danger')));c.dataset.providerSearch=[p.name,p.base_url,...p.models].join(' ').toLowerCase();if(!p.models.length)c.querySelectorAll('button').forEach(b=>{if(b.textContent.startsWith('用于'))b.disabled=true;});grid.append(c);}const filterProviders=()=>{localFilters({providerQuery:providerSearch.value});let count=0;for(const row of grid.children){row.hidden=!row.dataset.providerSearch.includes(providerSearch.value.trim().toLowerCase());if(!row.hidden)count++;}providerCounts.textContent=count+' / '+data.items.length+' 个提供商';};providerSearch.oninput=filterProviders;filterProviders();page.append(data.items.length?grid:empty('连接第一个模型','填写兼容接口地址和密钥后，就可以开始对话。密钥仅写入服务器，不会回传明文。',button('添加提供商',()=>providerForm(null),'primary')));return page;}
function providerForm(provider) {
  const p=provider||{name:'',base_url:'',models:[],api_format:'openai'};
  const form=el('form',{},field('提供商名称','name',p.name),field('接口地址','base_url',p.base_url,'url','填写 API 地址，不要把密钥放入地址中。'),
    select('接口格式','api_format',state.providerFormats||['openai','openai_compatible','claude','gemini','vertex'],p.api_format),
    field('API Key','api_key','','password',provider?'留空保留已保存密钥；不会读取或回显明文。':'只写入服务器，不保存到浏览器。'),
    check('清除已保存密钥','clear_key'),field('已保存模型（每行一个）','models',p.models.join('\n'),'textarea'));
  form.elements.api_key.autocomplete='new-password';
  const candidates=el('div',{class:'wb-model-candidates'});
  if(provider){const fetchBtn=button('拉取候选模型',async()=>{
    fetchBtn.disabled=true;fetchBtn.textContent='正在连接…';candidates.replaceChildren();
    try{const d=await wb('providers/fetch',{data:{name:provider.name}});
      const choices=el('div',{class:'wb-candidate-list'});const selected=new Set(form.elements.models.value.split('\n').map(s=>s.trim()));
      d.models.forEach(model=>choices.append(check(model,model,selected.has(model))));
      const search=el('input',{type:'search',placeholder:'筛选候选模型','aria-label':'筛选候选模型'});
      search.oninput=()=>choices.querySelectorAll('label').forEach(l=>l.hidden=!l.textContent.toLowerCase().includes(search.value.toLowerCase()));
      candidates.append(el('h3',{},`${d.models.length} 个候选模型`),el('p',{class:'wb-note'},'使用服务器已保存的地址和密钥。修改连接配置后请先保存，再拉取。'),search,choices,
        actions(button('全选可见',()=>choices.querySelectorAll('label:not([hidden]) input').forEach(i=>i.checked=true)),button('应用到列表',()=>{
          form.elements.models.value=[...choices.querySelectorAll('input:checked')].map(i=>i.name).join('\n');state.dirty=true;inlineState(candidates,'已应用到编辑列表，保存配置后生效。');
        })));
    }catch(e){showError(candidates,e);}finally{fetchBtn.disabled=false;fetchBtn.textContent='拉取候选模型';}});form.append(fetchBtn,candidates);}
  form.append(el('p',{class:'wb-note'},'删除当前正在使用的模型会清除对应默认选择，需要重新指定。'),actions(button('取消',closeDialog),el('button',{class:'wb-btn primary',type:'submit'},'保存配置')));
  dirtyForm(form);form.onsubmit=e=>{e.preventDefault();submit(form,async d=>{
    const data={...d,create:!provider,original_name:provider?.name,models:d.models.split('\n').map(v=>v.trim()).filter(Boolean)};
    if(!d.api_key)delete data.api_key;
    await wb('providers/save',{data});form.elements.api_key.value='';state.dirty=false;closeDialog();await refreshBootstrap();await render();
  });};dialog(provider?'编辑提供商':'添加提供商',form);
}
function importExport(){const form=el('div',{},el('p',{class:'wb-note'},'导入会合并/覆盖同名提供商；脱敏配置不覆盖原有密钥。默认导出不包含任何密钥。'));const file=el('input',{type:'file',accept:'.json,application/json','aria-label':'选择配置文件'});const errors=el('div');form.append(field('粘贴 JSON 配置','config','','textarea'),file,errors);file.onchange=async()=>{if(file.files[0])form.querySelector('textarea').value=await file.files[0].text();};form.append(actions(button('确认导入',async()=>{try{const config=JSON.parse(form.querySelector('textarea').value);if(!confirm('合并导入配置并覆盖同名提供商？'))return;await wb('providers/import',{data:{config,confirm:true}});form.querySelector('textarea').value='';closeDialog();render();await refreshBootstrap();}catch(e){errors.replaceChildren(message(e.message));}},'primary'),button('导出脱敏配置',async()=>{try{download('xgent-providers.json',await wb('providers/export',{data:{include_secrets:false}}));}catch(e){errors.replaceChildren(message(e.message));}}),button('导出含密钥配置',async()=>{if(!confirm('导出的文件包含明文 API Key。确认下载并自行妥善保管？'))return;try{download('xgent-providers-private.json',await wb('providers/export',{data:{include_secrets:true,confirm:true}}));}catch(e){errors.replaceChildren(message(e.message));}},'danger')));dialog('配置导入与导出',form);}
// Local skill filters don't fetch the same small catalogue or throw away focused controls.
function localFilters(values){
  const entry=pageCache.get(state.pageKey);pageCache.delete(state.pageKey);
  Object.assign(state.filters[state.route]??={},values);state.pageKey=pageKey();
  if(entry){pageCache.set(state.pageKey,entry.route,entry.content,entry.revision,root.scrollTop);Object.assign(pageCache.get(state.pageKey),entry);}
}
async function managementModule(name){
  try{return await import('./'+name+'.js');}
  catch{throw new Error('管理页面脚本加载失败。若刚更新代码，请重启 XGent 服务并刷新网页；聊天、菜单和退出登录仍可使用。');}
}
async function usagePage(read){const {usageView}=await managementModule('usage');return usageView(read,{filter,render,heading,card,actions,priceSettings,state});}
function priceSettings(){
  const form=el('form',{class:'wb-price-form'}),priceRows=el('div',{class:'wb-price-rows'}),mergeRows=el('div',{class:'wb-merge-rows'});
  function priceRow(name='',value={}){const row=el('div',{class:'wb-price-row'},field('模型名称','model',name),...['input','output','cached'].map((key,i)=>{const node=field(['输入单价','输出单价','缓存单价'][i],key,value[key]??0,'number');node.querySelector('input').min='0';node.querySelector('input').step='any';return node;}));row.append(button('移除',()=>{row.remove();form.dispatchEvent(new Event('input',{bubbles:true}));},'danger'));priceRows.append(row);}
  function mergeRow(name='',aliases=[]){const row=el('div',{class:'wb-merge-row'},field('合并后的模型名','model',name),field('原始名称（每行一个）','aliases',aliases.join('\n'),'textarea'));row.append(button('移除',()=>{row.remove();form.dispatchEvent(new Event('input',{bubbles:true}));},'danger'));mergeRows.append(row);}
  for(const [name,price] of Object.entries(state.boot?.settings.values.model_price_table||{}))priceRow(name,price);
  for(const [name,names] of Object.entries(state.boot?.settings.values.model_merge_map||{}))mergeRow(name,names);
  form.append(el('h3',{},'模型价格'),el('p',{class:'wb-note'},'单位：USD / 百万 Token。default 用作默认价格。'),priceRows,button('添加模型价格',()=>priceRow()),el('h3',{class:'wb-section-title'},'模型名称合并'),mergeRows,button('添加合并规则',()=>mergeRow()),el('div',{class:'wb-form-save'},el('button',{type:'submit',class:'wb-btn primary'},'保存规则')));dirtyForm(form);
  form.onsubmit=e=>{e.preventDefault();submit(form,async()=>{const prices={},merge={};for(const row of priceRows.children){const name=row.querySelector('[name=model]').value.trim();if(!name)throw new Error('请填写模型名称');if(prices[name])throw new Error('模型价格名称重复：'+name);prices[name]=Object.fromEntries(['input','output','cached'].map(key=>{const n=Number(row.querySelector('[name='+key+']').value);if(!Number.isFinite(n)||n<0)throw new Error('单价必须是非负数');return [key,n];}));}for(const row of mergeRows.children){const name=row.querySelector('[name=model]').value.trim();if(!name||merge[name])throw new Error('合并名称为空或重复');merge[name]=row.querySelector('[name=aliases]').value.split('\n').map(s=>s.trim()).filter(Boolean);}await wb('settings',{data:{key:'model_price_table',value:prices}});await wb('settings',{data:{key:'model_merge_map',value:merge}});state.dirty=false;closeDialog();await refreshBootstrap();render();});};dialog('价格与合并规则',form);
}

async function settingsPage(read){const {settingsView}=await managementModule('settings');
  const boot=await read('bootstrap');state.boot=boot;
  const health=filter('section')==='health'?await read.api('/api/health'):{};
  return settingsView({logout,boot,health,state,render,dialog,closeDialog,submit,dirtyForm,confirmAction,refreshBootstrap,setTheme,filter,heading,card,actions});
}
function setTheme(value){pageCache.invalidate(['settings']);if(value==='system')localStorage.removeItem('xgent-theme');else localStorage.setItem('xgent-theme',value);const dark=value==='dark'||value==='system'&&matchMedia('(prefers-color-scheme:dark)').matches;document.body.classList.toggle('dark',dark);const frame=$('terminal-iframe');frame?.contentWindow?.postMessage({type:'xgent-theme',dark},location.origin);}
const media=matchMedia('(prefers-color-scheme:dark)');media.addEventListener('change',()=>{if(!localStorage.getItem('xgent-theme'))setTheme('system');});document.body.classList.toggle('wb-reduce-motion',localStorage.getItem('xgent-reduce-motion')==='1');
// Desktop inspector width. No modal blocking background reading on wide screens.
const resize=$('wb-resizer');let resizing=false;resize.onpointerdown=e=>{resizing=true;resize.setPointerCapture(e.pointerId);};resize.onpointermove=e=>{if(!resizing)return;const width=Math.max(320,Math.min(700,window.innerWidth-e.clientX));document.documentElement.style.setProperty('--detail-width',width+'px');};resize.onpointerup=()=>{resizing=false;localStorage.setItem('xgent-detail-width',getComputedStyle(document.documentElement).getPropertyValue('--detail-width'));};resize.onkeydown=e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();const width=parseInt(getComputedStyle(document.documentElement).getPropertyValue('--detail-width'))+(e.key==='ArrowLeft'?20:-20);document.documentElement.style.setProperty('--detail-width',Math.max(320,Math.min(700,width))+'px');}};const width=localStorage.getItem('xgent-detail-width');if(/^\d+px$/.test(width||''))document.documentElement.style.setProperty('--detail-width',width);
// Terminal is retained when hidden, so switching workspaces never sends a kill operation.
new MutationObserver(()=>document.body.classList.toggle('wb-terminal-open',$('terminal-modal').classList.contains('open'))).observe($('terminal-modal'),{attributes:true,attributeFilter:['class']});
// Composer measurement also handles software keyboards and multiline drafts.
new ResizeObserver(()=>document.documentElement.style.setProperty('--footer-height',($('composer').parentElement.offsetHeight||118)+'px')).observe($('composer').parentElement);
window.visualViewport?.addEventListener('resize',()=>{const keyboard=window.innerHeight-window.visualViewport.height>140;document.body.classList.toggle('wb-keyboard',keyboard);$('wb-shell').style.height=keyboard?window.visualViewport.height+'px':'';});
let started=false;async function start(){if(started)return;const epoch=state.authEpoch;started=true;try{await refreshBootstrap();if(epoch!==state.authEpoch)return;state.authenticated=true;await route();}catch(e){if(epoch!==state.authEpoch)return;started=false;if(e.status!==401&&e.name!=='AbortError')$('wb-history-state').textContent='工作台配置读取失败：'+e.message;}}
window.addEventListener('xgent-authenticated',start);
window.addEventListener('xgent-auth-expired',()=>{
  state.authEpoch++;started=false;state.authenticated=false;state.request?.abort();state.renderSequence++;
  state.boot=null;state.pendingKey=null;state.pageKey=null;state.dirty=false;state.retryAfter=0;
  pageCache.clear();root.replaceChildren();closeDetail(true);
  if($('wb-dialog').open)$('wb-dialog').close();$('wb-dialog-body').replaceChildren();
  $('login').showModal();
});
api('/api/session').then(data=>{if(data.authenticated)start();}).catch(()=>{});route();
// Refresh only the visible stale page. Fast navigation reuses warm DOM without requests.
setInterval(backgroundRefresh,15000);

document.addEventListener('visibilitychange',()=>{if(document.hidden){state.request?.abort();state.pendingKey=null;}else backgroundRefresh();});
document.addEventListener('keydown',e=>{if(e.key!=='Tab'||$('wb-detail').hidden||innerWidth>=1200||$('wb-dialog').open)return;const nodes=[...$('wb-detail').querySelectorAll('button,a,input,select,textarea,[tabindex]')].filter(n=>!n.disabled&&n.offsetParent);if(!nodes.length)return;const first=nodes[0],last=nodes.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}});

new MutationObserver(()=>{$('terminal-iframe')?.contentWindow?.postMessage({type:'xgent-theme',dark:document.body.classList.contains('dark')},location.origin);}).observe(document.body,{attributes:true,attributeFilter:['class']});

// Basic chat actions have their own handlers; enhance them only after this
// entire optional workbench initialized successfully.
window.XGentWorkbench={
  dialog,closeDialog,
  prepareChat:async()=>{if(state.route!=='chat'||location.hash.includes('?')){location.hash='/chat';await route();}return state.route==='chat';},
  commands:()=>state.boot?.commands,
  confirmCommand:cmd=>confirmAction('执行 '+cmd,'此命令可能改变共享数据或重启服务，是否继续？',async()=>window.XGentChat.command(cmd)),
  beforeLogout:()=>{if(state.submitting||state.detailSaving)return false;if((state.dirty||state.detailDirty)&&!confirm('尚有未保存修改，确定退出登录？'))return false;state.dirty=false;state.detailDirty=false;return true;}
};
window.dispatchEvent(new Event('xgent-workbench-ready'));

window.addEventListener('xgent-conversation-changed',()=>{
  if(state.navigatingSource){pageCache.invalidate(['tasks','files']);return;}
  if(['models','settings'].includes(state.route))return; // Shared document/configuration drafts are not chat-bound.
  state.request?.abort();state.detailRequest?.abort();state.renderSequence++;
  pageCache.clear();state.pageKey=null;state.pendingKey=null;state.retryAfter=0;
  closeDetail();if(state.route!=='chat'&&state.authenticated)render();
});

window.addEventListener('xgent-fonts-applied',()=>{
  if(state.dirty||state.submitting)return;
  const values=window.XGentAppearance?.state.values||{};
  for(const input of document.querySelectorAll('.font-settings input[name]'))if(input.name in values)input.value=values[input.name];
  for(const slider of document.querySelectorAll('.font-settings [data-font-key]'))if(slider.dataset.fontKey in values)slider.value=values[slider.dataset.fontKey];
});

function taskSourceSearch(){
  const input=el('input',{type:'search',placeholder:'搜索对话名称或历史内容…','aria-label':'搜索来源对话'}),results=el('div',{class:'wb-source-results'});
  const box=el('div',{class:'wb-source-dialog'},el('label',{class:'wb-field'},el('span',{},'对话名称或历史内容'),input),el('p',{class:'wb-note'},'选择结果只筛选来源，不切换当前聊天。'),results);
  let controller,timer,sequence=0;
  async function search(){
    controller?.abort();controller=new AbortController();const current=++sequence;results.replaceChildren(message('正在搜索…','loading'));
    try{
      const data=await wb('conversations/search'+query({q:input.value}),{signal:controller.signal});
      if(current!==sequence||!box.isConnected)return;
      results.replaceChildren();
      for(const item of data.items){const choose=button('',()=>{filter('source',item.id);filter('offset',0);state.dirty=false;closeDialog();render();});choose.classList.add('wb-source-result');choose.append(el('strong',{},item.name||'新对话'),el('span',{class:'wb-note'},item.id.slice(0,6)+(item.archived?' · 已归档':'')));if(item.snippet)choose.append(el('p',{},item.snippet));results.append(choose);}
      if(!data.items.length)results.append(empty('没有找到对话','试试会话名称或消息中的关键词。'));
    }catch(error){if(error.name!=='AbortError'&&current===sequence)results.replaceChildren(message(error.message),button('重新搜索',search));}
  }
  input.oninput=()=>{clearTimeout(timer);controller?.abort();sequence++;results.replaceChildren(message('正在搜索…','loading'));timer=setTimeout(search,250);};
  dialog('搜索来源对话',box);search();
  $('wb-dialog').addEventListener('close',()=>{clearTimeout(timer);controller?.abort();},{once:true});
}
