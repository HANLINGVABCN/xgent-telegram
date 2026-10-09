const defaults = {web_message_font_size:14, web_ui_font_size:13, web_terminal_font_size:13};
const limits = {web_message_font_size:[12,24], web_ui_font_size:[12,20], web_terminal_font_size:[10,24]};
let state = {revision:-1, values:{...defaults}}, preview = null;
function apply(values) {
  const names = {web_message_font_size:'--web-message-font-size',web_ui_font_size:'--web-ui-font-size',web_terminal_font_size:'--web-terminal-font-size'};
  for (const key of Object.keys(defaults)) document.documentElement.style.setProperty(names[key], values[key]+'px');
  window.dispatchEvent(new CustomEvent('xgent-fonts-applied',{detail:values}));
  document.getElementById('terminal-iframe')?.contentWindow?.postMessage({type:'xgent-fonts',values},location.origin);
}
function accept(data) {
  if (!data?.values || !Number.isSafeInteger(data.revision) || data.revision<state.revision) return;
  const values={...state.values};
  for (const [key,[min,max]] of Object.entries(limits)) {
    const value=data.values[key]; if(Number.isInteger(value)&&value>=min&&value<=max)values[key]=value;
  }
  state={revision:data.revision,values}; if(!preview)apply(values);
}
async function refresh(){const response=await fetch('/api/config',{credentials:'same-origin',cache:'no-store'});if(response.ok)accept(await response.json());}
async function save(values){
  for(const key of Object.keys(defaults)) {
    if(values[key]===undefined||values[key]===state.values[key])continue;
    const response=await fetch('/api/config',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,value:Number(values[key])})});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'字号保存失败');accept(data);
  }
  preview=null;apply(state.values);return state;
}
window.XGentAppearance={accept,refresh,save,defaults,limits,get state(){return state;},preview(values){preview={...state.values,...values};apply(preview);},cancelPreview(){preview=null;apply(state.values);}};
window.addEventListener('xgent-authenticated',()=>refresh().catch(()=>{}));
window.addEventListener('xgent-auth-expired',()=>{preview=null;state={revision:-1,values:{...defaults}};apply(defaults);});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh().catch(()=>{});});
apply(defaults);
fetch('/api/session',{credentials:'same-origin'}).then(r=>r.json()).then(s=>{if(s.authenticated)return refresh();}).catch(()=>{});

setInterval(()=>{if(!document.hidden&&state.revision>=0)refresh().catch(()=>{});},15000);
