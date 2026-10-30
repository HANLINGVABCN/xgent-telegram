/* Optional Telegram host controls. Browser behavior remains the same. */
export function installTelegramHost({kind='workbench',input=null,getConversation=()=>null}={}){
  const tg=window.Telegram?.WebApp;if(!tg?.initData)return;
  const supports=version=>!!tg.isVersionAtLeast?.(version);
  function theme(){
    if(!localStorage.getItem('xgent-theme'))document.body.classList.toggle('dark',tg.colorScheme==='dark');
    document.documentElement.style.setProperty('--tg-safe-top',(tg.contentSafeAreaInset?.top||0)+'px');
    document.documentElement.style.setProperty('--tg-safe-bottom',(tg.contentSafeAreaInset?.bottom||0)+'px');
    try{if(supports('6.1')){tg.setHeaderColor('bg_color');tg.setBackgroundColor('bg_color');}}catch{}
  }
  theme();for(const event of ['themeChanged','safeAreaChanged','contentSafeAreaChanged'])tg.onEvent(event,theme);
  const bar=document.getElementById('bar')||document.getElementById('header')||document.querySelector('header');
  function action(label,run){if(!bar)return;const button=document.createElement('button');button.type='button';button.textContent=label;button.style.cssText='font-size:12px;padding:4px 7px;opacity:.7';button.onclick=run;bar.append(button);}
  if(supports('8.0')){action('全屏',()=>tg.isFullscreen?tg.exitFullscreen():tg.requestFullscreen());if(kind==='workbench')action('加入桌面',()=>tg.addToHomeScreen());}
  if(!input||!supports('9.0')||!tg.DeviceStorage)return;
  let serial=0;
  const key=()=>getConversation()?'draft_'+getConversation():null;
  function save(){const target=key();if(target)tg.DeviceStorage.setItem(target,input.value);}
  function load(){const target=key(),stamp=serial;if(!target||input.value)return;
    tg.DeviceStorage.getItem(target,(error,value)=>{if(!error&&target===key()&&stamp===serial&&!input.value&&value){input.value=value;input.dispatchEvent(new Event('input',{bubbles:true}));}});}
  input.addEventListener('input',()=>{serial++;save();});
  window.addEventListener('xgent-conversation-changed',()=>queueMicrotask(load));
  window.addEventListener('beforeunload',save);document.addEventListener('visibilitychange',()=>{if(document.hidden)save();});
  load();
}
