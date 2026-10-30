import {syncAskForm} from './ask-form.js';

const tg=window.Telegram?.WebApp;
const query=new URLSearchParams(location.search);
const page=document.body.dataset.page||'form';
const status=document.getElementById('page-status');
const retry=document.getElementById('page-retry');
const entry={bubble:document.getElementById('content')};
let token='',loaded=false;
let rawText='';
const supported=version=>!!tg?.isVersionAtLeast?.(version);

function theme(){
  document.body.classList.toggle('dark',tg?.colorScheme==='dark');
  const colors=tg?.themeParams||{};
  for(const [key,value] of Object.entries({surface:colors.bg_color,text:colors.text_color,'text-3':colors.hint_color,accent:colors.button_color,'surface-2':colors.secondary_bg_color}))
    if(/^#[0-9a-f]{6}$/i.test(value||''))document.documentElement.style.setProperty('--'+key,value);
  const top=Math.max(tg?.safeAreaInset?.top||0,tg?.contentSafeAreaInset?.top||0);
  const bottom=Math.max(tg?.safeAreaInset?.bottom||0,tg?.contentSafeAreaInset?.bottom||0);
  document.documentElement.style.setProperty('--safe-top',top+'px');
  document.documentElement.style.setProperty('--safe-bottom',bottom+'px');
  try{if(supported('6.1')){tg.setHeaderColor('bg_color');tg.setBackgroundColor('bg_color');}if(supported('7.10'))tg.setBottomBarColor('bg_color');}catch{}
}

async function api(path,body){
  const response=await fetch('/api/mini/'+path,{method:body?'POST':'GET',credentials:'omit',
    headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:body?JSON.stringify(body):undefined});
  const data=await response.json();if(!response.ok){const error=new Error(data.error||'请求失败');error.status=response.status;throw error;}return data;
}

function paintNative(state){
  if(!tg)return;
  const closed=state.snapshot.status!=='pending';
  status.textContent=closed?{submitted:'已提交',cancelled:'已取消',expired:'已失效'}[state.snapshot.status]||'已结束':state.busy?'正在保存…':'填写后提交';
  try{
    if(supported('9.0')&&tg.DeviceStorage){
      const key='form_'+state.snapshot.conversation_id+'_'+state.snapshot.id;
      if(!state.dirty||closed)tg.DeviceStorage.removeItem(key);
      else{const draft={};for(const question of state.snapshot.questions)if(question.type!=='secret')draft[question.id]=state.draft[question.id]||{};tg.DeviceStorage.setItem(key,JSON.stringify({revision:state.snapshot.revision,draft}));}
    }
    const unsaved=state.dirty||state.fields.some(field=>field.q.type==='secret'&&field.input.value);
    if(supported('6.2')){if(!closed&&unsaved)tg.enableClosingConfirmation();else tg.disableClosingConfirmation();}
    if(supported('6.1')){
      if(closed)tg.MainButton.hide();
      else{tg.MainButton.setText('提交').show();state.busy?tg.MainButton.disable().showProgress():tg.MainButton.hideProgress().enable();}
    }
    if(supported('7.10')){
      if(closed)tg.SecondaryButton.hide();
      else{tg.SecondaryButton.setText('取消').show();state.busy?tg.SecondaryButton.disable():tg.SecondaryButton.enable();}
    }
  }catch{}
}

async function boot(){
  status.textContent='正在打开…';retry.hidden=true;
  try{
    if(!tg?.initData)throw new Error('请从 Telegram Bot 的按钮打开此页面');
    const session=await api('session',{init_data:tg.initData,page,id:query.get('id'),conversation_id:query.get('conversation_id')});
    token=session.token;
    if(page!=='form'){
      renderResource(session.snapshot);
      try{if(supported('6.1'))tg.BackButton.show().onClick(()=>tg.close());}catch{}
      return;
    }
    const draftKey='form_'+session.snapshot.conversation_id+'_'+session.snapshot.id;
    if(supported('9.0')&&tg.DeviceStorage){
      const stored=await new Promise(resolve=>{const timeout=setTimeout(()=>resolve(null),500);tg.DeviceStorage.getItem(draftKey,(error,value)=>{clearTimeout(timeout);resolve(error?null:value);});});
      try{const cached=JSON.parse(stored||'null');if(cached?.revision===session.snapshot.revision&&cached.draft)for(const question of session.snapshot.questions)if(question.type!=='secret'&&cached.draft[question.id])session.snapshot.draft[question.id]=cached.draft[question.id];}catch{}
    }
    syncAskForm(entry,session.snapshot,{nativeButtons:supported('6.1'),request:body=>api('form',body),load:()=>api('form'),
      onState:paintNative,onSettled:()=>{
        try{if(supported('6.1'))tg.HapticFeedback.notificationOccurred('success');if(supported('9.1'))tg.hideKeyboard();tg.disableClosingConfirmation();tg.close();}catch{}
      }});
    if(!loaded){
      loaded=true;
      try{
        if(supported('6.1')){tg.MainButton.onClick(()=>entry.askState?.submit());tg.BackButton.show().onClick(()=>tg.close());}
        if(supported('7.10'))tg.SecondaryButton.onClick(()=>entry.askState?.cancel());
      }catch{}
      setInterval(async()=>{if(!entry.askState||entry.askState.dirty||entry.askState.busy||entry.askState.snapshot.status!=='pending')return;
        try{syncAskForm(entry,await api('form'));}catch(error){if(error.status===401){status.textContent='重新打开可继续填写';retry.hidden=false;}}
      },3000);
    }
  }catch(error){status.textContent=error.message;retry.hidden=false;}
}

retry.onclick=boot;
theme();try{tg?.ready();tg?.expand();for(const event of ['themeChanged','safeAreaChanged','contentSafeAreaChanged','viewportChanged'])tg?.onEvent(event,theme);}catch{}
boot();

function button(text,click){const node=document.createElement('button');node.type='button';node.textContent=text;node.onclick=async()=>{node.disabled=true;try{await click();}catch(error){status.textContent=error.message;}finally{node.disabled=false;}};return node;}
function input(label,value,type='text'){const node=document.createElement('label');node.textContent=label;const field=document.createElement('input');field.type=type;field.value=value??'';if(type==='password')field.autocomplete='off';node.append(field);entry.bubble.append(node);return field;}
async function reloadResource(params={}){renderResource(await api('resource?'+new URLSearchParams(params)));}
async function saveResource(body){const data=await api('resource',body);status.textContent='已保存';return data;}
function renderResource(data){
  document.getElementById('page-title').textContent={read:'阅读回复',settings:'设置与模型',tasks:'任务',files:'文件'}[page]||'XGent';status.textContent='';entry.bubble.replaceChildren();
  if(page==='read'){
    rawText=data.text||'';const tools=document.createElement('div');tools.className='mini-toolbar';
    if(supported('8.0'))tools.append(button('全屏',()=>tg.isFullscreen?tg.exitFullscreen():tg.requestFullscreen()));
    tools.append(button('复制全文',async()=>{await navigator.clipboard.writeText(rawText);status.textContent='已复制';}),button('分享',async()=>{const prepared=await api('share',{});if(supported('8.0'))tg.shareMessage(prepared.id);else throw new Error('请更新 Telegram 后使用原生分享');}));
    const body=document.createElement('pre');body.className='mini-reply';body.textContent=rawText;entry.bubble.append(tools,body);return;
  }
  if(page==='settings'){
    const settings=data.settings?.values||data.settings||{};
    const mode=document.createElement('select');for(const [value,label] of [['foreground','前台流式'],['background','后台流式'],['nonstream','非流式']]){const option=document.createElement('option');option.value=value;option.textContent=label;mode.append(option);}mode.value=settings.response_mode||'foreground';
    entry.bubble.append(mode,button('保存回复方式',()=>saveResource({key:'response_mode',value:mode.value})));
    for(const [key,label] of [['agent_mode','Agent 模式'],['telegram_reactions','消息反应']]){const field=input(label,null,'checkbox');field.checked=!!settings[key];field.onchange=()=>saveResource({key,value:field.checked}).catch(error=>status.textContent=error.message);}
    const depth=input('记忆深度',settings.global_depth??30,'number');entry.bubble.append(button('保存深度',()=>saveResource({key:'global_depth',value:Number(depth.value)})));
    const providers=data.providers?.items||[];const provider=document.createElement('select');for(const item of providers){const option=document.createElement('option');option.value=item.name;option.textContent=item.name;provider.append(option);}
    const model=document.createElement('select');function models(){model.replaceChildren();for(const name of providers.find(item=>item.name===provider.value)?.models||[]){const option=document.createElement('option');option.value=name;option.textContent=name;model.append(option);}}provider.onchange=models;models();
    const target=document.createElement('select');for(const [value,label] of [['chat','对话模型'],['media','媒体模型']]){const option=document.createElement('option');option.value=value;option.textContent=label;target.append(option);}
    entry.bubble.append(provider,model,target,button('使用此模型',()=>saveResource({action:'select_model',target:target.value,provider:provider.value,model:model.value})));
    const title=document.createElement('h2');title.textContent='提供商配置';entry.bubble.append(title);
    const name=input('名称','');const base=input('接口地址','','url');const key=input('API Key','','password');const names=input('模型名称，逗号分隔','');
    entry.bubble.append(button('保存提供商',async()=>{await saveResource({action:'save_provider',name:name.value,base_url:base.value,api_key:key.value,models:names.value.split(',').map(value=>value.trim()).filter(Boolean),api_format:'openai'});key.value='';await reloadResource();}),button('扫码填入接口地址',async()=>{if(!supported('6.4'))throw new Error('此客户端不支持扫码');tg.showScanQrPopup({text:'扫描接口地址'},text=>{base.value=text;return true;});}));return;
  }
  const search=document.createElement('input');search.type='search';search.placeholder='搜索';entry.bubble.append(search,button('查询',()=>reloadResource({q:search.value})));
  for(const item of data.items||[]){const row=document.createElement('section');row.className='mini-item';const name=document.createElement('strong');name.textContent=item.filename||item.name||String(item.id);row.append(name);const detail=document.createElement('span');detail.textContent=page==='tasks'?(item.status||''):(item.size?Math.round(item.size/1024)+' KB':'');row.append(detail);
    if(page==='tasks')row.append(button('取消任务',async()=>{await saveResource({action:'cancel',ids:[item.id],confirm:true});await reloadResource();}));
    else row.append(button('下载',async()=>{const file=await api('file-ticket',{id:item.id,kind:item.kind==='output'?'outputs':'files'});if(supported('8.0'))tg.downloadFile(file);else window.open(file.url,'_blank','noopener');}));entry.bubble.append(row);
  }
  if(!(data.items||[]).length){const empty=document.createElement('p');empty.textContent=page==='files'?'暂无文件':'暂无任务';entry.bubble.append(empty);}
}
