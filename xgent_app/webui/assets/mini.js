import {syncAskForm} from './ask-form.js';

const tg=window.Telegram?.WebApp;
const query=new URLSearchParams(location.search);
const page=document.body.dataset.page||'form';
const status=document.getElementById('page-status');
const retry=document.getElementById('page-retry');
const entry={bubble:document.getElementById('content')};
let token='',loaded=false;
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
