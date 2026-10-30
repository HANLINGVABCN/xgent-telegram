/* Native Web ask-x form; plain drafts survive reload, passwords stay in this DOM. */
export function syncAskForm(entry,snapshot,options={}){
  if(!snapshot||snapshot.version!==1)return;
  if(entry.askState?.snapshot.id===snapshot.id){
    const state=entry.askState;
    if(snapshot.revision<state.snapshot.revision)return;
    state.snapshot=snapshot;
    if(!state.dirty&&!state.busy)state.draft=structuredClone(snapshot.draft||{});
    state.paint();return;
  }
  entry.askNode?.remove();
  entry.btns?.remove();entry.btns=null;
  const node=document.createElement('form');node.className='web-ask-form';node.setAttribute('aria-label','Agent 提问表单');
  const key='xgent-ask-draft:'+snapshot.conversation_id+':'+snapshot.id;
  const state={snapshot,draft:structuredClone(snapshot.draft||{}),dirty:false,busy:false,serial:0,timer:null,writes:Promise.resolve(),fields:[],settled:false};
  entry.askState=state;entry.askNode=node;
  try{
    const local=JSON.parse(sessionStorage.getItem(key)||'null');
    if(local?.revision===snapshot.revision&&local.draft){state.draft=local.draft;state.dirty=true;}
    else if(local)sessionStorage.removeItem(key);
  }catch{}
  const saveLocal=()=>{const safe={};for(const q of snapshot.questions)if(q.type!=='secret')safe[q.id]=state.draft[q.id]||{};try{sessionStorage.setItem(key,JSON.stringify({revision:state.snapshot.revision,draft:safe}));}catch{}};
  const notice=document.createElement('p');notice.className='web-ask-notice';notice.setAttribute('role','status');
  async function request(action){
    clearTimeout(state.timer);
    state.writes=state.writes.catch(()=>{}).then(async()=>{
      if(state.snapshot.status!=='pending')return;
      const draft={};for(const q of state.snapshot.questions)if(q.type!=='secret')draft[q.id]=structuredClone(state.draft[q.id]||{});
      const serial=state.serial;
      state.busy=true;state.paint();notice.textContent=action==='submit'?'正在提交…':action==='cancel'?'正在取消…':'正在保存…';
      const secrets={};if(action==='submit')for(const field of state.fields)if(field.q.type==='secret'&&field.input.value)secrets[field.q.id]=field.input.value;
      try{
        const payload={id:state.snapshot.id,conversation_id:state.snapshot.conversation_id,revision:state.snapshot.revision,action,draft,secrets};
        let data;
        if(options.request)data=await options.request(payload);
        else{
          const response=await fetch('/api/workbench/ask',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
          data=await response.json();if(!response.ok)throw new Error(data.error||'表单保存失败');
        }
        state.snapshot=data;
        if(serial===state.serial){state.dirty=false;state.draft=structuredClone(data.draft||{});sessionStorage.removeItem(key);}
        else saveLocal();
        if(data.status!=='pending'){sessionStorage.removeItem(key);for(const field of state.fields)if(field.q.type==='secret')field.input.value='';}
        notice.textContent=data.status==='submitted'?'已提交':data.status==='cancelled'?'已取消':'已保存';
      }catch(error){
        notice.textContent=error.message;
        try{
          if(options.load)state.snapshot=await options.load();
          else{const latest=await fetch('/api/workbench/ask?id='+encodeURIComponent(state.snapshot.id)+'&conversation_id='+encodeURIComponent(state.snapshot.conversation_id),{credentials:'same-origin'});if(latest.ok)state.snapshot=await latest.json();}
        }catch{}
      }finally{
        state.busy=false;state.paint();
        if(action!=='save'&&state.snapshot.status!=='pending'&&!state.settled){state.settled=true;options.onSettled?.(state.snapshot);}
      }
    });
    return state.writes;
  }
  function changed(){state.dirty=true;state.serial++;notice.textContent='正在保存…';saveLocal();state.paint();clearTimeout(state.timer);state.timer=setTimeout(()=>request('save'),400);}
  for(const q of snapshot.questions){
    const group=document.createElement('fieldset');group.className='web-ask-question';
    const legend=document.createElement('legend');legend.textContent=(q.label?q.label+' · ':'')+q.prompt;group.append(legend);
    const field={q,group,options:[]};state.fields.push(field);
    if(q.type==='single'||q.type==='multi'){
      group.setAttribute('role',q.type==='single'?'radiogroup':'group');group.setAttribute('aria-label',q.prompt);
      q.options.forEach((text,index)=>{
        const button=document.createElement('button');button.type='button';button.className='web-ask-choice';button.setAttribute('role',q.type==='single'?'radio':'checkbox');
        const mark=document.createElement('span');mark.className='web-ask-check';mark.setAttribute('aria-hidden','true');
        const label=document.createElement('span');label.textContent=text;button.append(mark,label);
        button.onclick=()=>{const answer=state.draft[q.id]||={};const selected=new Set(answer.selected||[]);
          if(q.type==='single'){answer.selected=[index];answer.custom='';}
          else{selected.has(index)?selected.delete(index):selected.add(index);answer.selected=[...selected].sort();}
          changed();};field.options.push({button,mark,index});group.append(button);
      });
    }
    if(q.type==='text'||q.type==='secret'||q.other){
      const label=document.createElement('label');label.className='web-ask-input';
      const title=document.createElement('span');title.textContent=q.type==='secret'?'密钥 $'+q.var:q.type==='text'?'填写答案':'自定义答案';
      const input=document.createElement(q.type==='secret'?'input':'textarea');
      if(q.type==='secret'){input.type='password';input.autocomplete='off';}else{input.rows=2;input.value=state.draft[q.id]?.custom||'';}
      input.setAttribute('aria-label',q.prompt+(q.other?' · 自定义答案':''));input.maxLength=20000;
      input.oninput=()=>{if(q.type==='secret'){state.paint();return;}const answer=state.draft[q.id]||={};answer.custom=input.value;if(q.type==='single')answer.selected=[];changed();};
      label.append(title,input);group.append(label);field.input=input;
    }
    node.append(group);
  }
  const actions=document.createElement('div');actions.className='web-ask-actions';
  const submit=document.createElement('button');submit.type='submit';submit.textContent='提交';
  const cancel=document.createElement('button');cancel.type='button';cancel.textContent='取消';cancel.onclick=()=>request('cancel');
  actions.append(submit,cancel);node.append(actions,notice);
  node.onsubmit=event=>{event.preventDefault();request('submit');};
  state.paint=()=>{
    const closed=state.snapshot.status!=='pending';
    for(const field of state.fields){
      field.group.disabled=closed;
      for(const option of field.options){const checked=(state.draft[field.q.id]?.selected||[]).includes(option.index);option.button.setAttribute('aria-checked',String(checked));option.mark.textContent=checked?'✓':'';option.button.disabled=closed||state.busy;}
      if(field.input&&field.q.type!=='secret'&&document.activeElement!==field.input)field.input.value=state.draft[field.q.id]?.custom||'';
      if(field.input&&field.q.type==='secret'&&state.snapshot.draft[field.q.id]?.filled)field.input.placeholder='✓ 已录入';
    }
    actions.hidden=closed||!!options.nativeButtons;submit.disabled=cancel.disabled=state.busy;
    if(closed){clearTimeout(state.timer);sessionStorage.removeItem(key);for(const field of state.fields)if(field.q.type==='secret')field.input.value='';notice.textContent={submitted:'已提交',cancelled:'已取消',expired:'表单已失效，请让 AI 重新发起'}[state.snapshot.status]||'已结束';}
    options.onState?.(state);
  };
  state.submit=()=>request('submit');state.cancel=()=>request('cancel');
  entry.bubble.insertBefore(node,entry.footer||null);state.paint();
}
