import {el,button,field,wb,query,message,empty,tag} from './components.js';

const tick=String.fromCharCode(96);
export function splitSkillText(text){
  const lines=String(text).match(/[^\n]*\n|[^\n]+$/g)||[],excluded=new Set(),summaries=[];
  let start=-1,fence=0,summary=false;
  for(let i=0;i<lines.length;i++){
    const line=lines[i].trim().replace(/^\uFEFF/,'');
    if(fence){
      if(new RegExp('^'+tick+'{'+fence+',}\\s*$').test(line)){
        if(summary){for(let j=start;j<=i;j++)excluded.add(j);summaries.push(lines.slice(start+1,i).join('').replace(/^[\r\n]+|[\r\n]+$/g,''));}
        start=-1;fence=0;summary=false;
      }
    }else{
      const match=line.match(/^([\x60]{3,})([^\x60]*)$/);
      if(match){start=i;fence=match[1].length;summary=match[2].trim().split(/\s+/)[0]==='!';}
    }
  }
  return {summary:summaries.join('\n\n'),body:lines.filter((_,i)=>!excluded.has(i)).join('').replace(/^[\r\n]+|[\r\n]+$/g,'')};
}
export function composeSkillText(summary,body){
  if(!summary)return body;
  const length=(summary.match(/[\x60]+/g)||[]).reduce((max,value)=>Math.max(max,value.length+1),3);
  const fence=tick.repeat(length);
  return fence+'!\n'+summary+'\n'+fence+(body?'\n\n'+body:'\n');
}
function markdown(node,text,placeholder='暂无内容'){
  node.replaceChildren();
  if(!text){node.append(el('p',{class:'wb-note'},placeholder));return;}
  if(window.XGentChat?.render)node.innerHTML=window.XGentChat.render(text);
  else node.append(el('pre',{},text));
}
function kindName(kind){return kind==='skills'?'技能':'记忆';}
function sameDocument(ctx,kind,path){return ctx.state.detailDocument?.kind===kind&&ctx.state.detailDocument?.path===path;}

export async function knowledgePage(page,data,kind,ctx){
  const skillMode=kind==='skills',noun=kindName(kind),items=data.items||[];
  page.classList.add(skillMode?'wb-skills-page':'wb-memories-page');
  const search=el('input',{type:'search',placeholder:skillMode?'搜索技能名称、路径或简介':'搜索记忆名称或内容','aria-label':skillMode?'搜索技能名称或路径':'搜索记忆名称或内容'});
  search.value=ctx.filter(skillMode?'q':'memoryQuery')||'';
  const states=el('select',{'aria-label':'技能状态筛选'},[{value:'',label:'全部状态'},...['enabled','disabled','hidden'].map(value=>({value,label:tag(value).textContent}))].map(o=>el('option',{value:o.value},o.label)));
  states.value=ctx.filter('skillState')||'';
  const sources=el('select',{'aria-label':'技能来源筛选'},el('option',{value:''},'全部来源'),el('option',{value:'public'},'公共技能'),el('option',{value:'private'},'私有技能'));
  sources.value=ctx.filter('source')||'';
  const sort=el('select',{'aria-label':noun+'排序'},el('option',{value:'name'},'按名称排序'),el('option',{value:'source'},skillMode?'按来源排序':'按路径排序'));sort.value=ctx.filter(skillMode?'skillSort':'memorySort')||'name';
  const counts=el('span',{class:'wb-note',role:'status'}),feedback=el('div',{'aria-live':'polite'}),grid=el('div',{class:'wb-grid wb-skill-grid'}),cards=[];
  const noMatch=empty('没有匹配的'+noun,'换一个关键词或筛选条件试试。',button('清除筛选',()=>{search.value='';states.value='';sources.value='';applyFilters();}));
  function applyFilters(save=true){
    if(save)ctx.localFilters(skillMode?{q:search.value,skillState:states.value,source:sources.value}:{memoryQuery:search.value});
    const q=search.value.trim().toLocaleLowerCase();let visible=0;
    for(const [item,card] of cards){card.hidden=!((!q||[item.name,item.path,item.summary,item.preview].join(' ').toLocaleLowerCase().includes(q))&&(!skillMode||(!states.value||states.value===item.state)&&(!sources.value||sources.value===item.source)));if(!card.hidden)visible++;}
    const ordered=cards.slice().sort(([a],[b])=>String(sort.value==='source'?(a.source||a.path):a.name).localeCompare(String(sort.value==='source'?(b.source||b.path):b.name),'zh-CN'));for(const [,card] of ordered)grid.append(card);
    noMatch.hidden=visible>0;counts.textContent=visible+' / '+items.length+' 个'+noun+(skillMode?' · 已启用 '+items.filter(item=>item.state==='enabled').length:' · 所有对话共享');
  }
  search.oninput=()=>applyFilters();states.onchange=()=>applyFilters();sources.onchange=()=>applyFilters();sort.onchange=()=>{ctx.localFilters({[skillMode?'skillSort':'memorySort']:sort.value});applyFilters();};
  const create=button('新建'+noun,async()=>{
    if(create.disabled)return;
    create.disabled=true;
    try{
      const result=await wb(kind+'/create',{data:{}});
      ctx.localFilters(skillMode?{q:'',skillState:'',source:''}:{memoryQuery:''});
      await ctx.render();const fresh=Array.from(document.querySelectorAll('.knowledge-card')).find(card=>(kind==='skills'?card.dataset.skillPath:card.dataset.memoryPath)===result.item.path);await documentEditor(result.item,kind,ctx,{isNew:true,onSaved:fresh?._updateKnowledge});
    }catch(error){feedback.replaceChildren(message('新建失败：'+error.message));}
    finally{create.disabled=false;}
  },'primary');
  page.append(ctx.toolbar(search,...(skillMode?[states,sources]:[]),sort,button('清除筛选',()=>{search.value='';states.value='';sources.value='';applyFilters();}),create),counts,
    el('p',{class:'wb-note'},skillMode?'新建技能立即保存为空白私有 .md 文件，默认关闭。简介用于技能索引，正文可留空；文件名可在卡片上直接修改。':'手工记忆与所有对话共享，保存后在后续对话中读取；它不是聊天历史或压缩摘要。'),feedback,grid,noMatch);
  for(const item of items){
    const card=el('section',{class:'wb-card wb-skill-card knowledge-card',[skillMode?'data-skill-path':'data-memory-path']:item.path});
    const name=el('h3',{},item.name),badge=el('span'),result=el('div',{'aria-live':'polite'}),pathText=el('p',{class:'wb-skill-path',title:item.path},item.path);
    const preview=el('p',{class:'knowledge-card-preview'},item.summary||item.preview||(skillMode?'空白技能，点击管理填写简介与正文。':'空白记忆，点击查看 / 修改填写内容。'));
    const renameArea=el('div',{hidden:true,class:'knowledge-rename-area'});
    const rename=button('✎',()=>{
      if(sameDocument(ctx,kind,item.path)&&!ctx.closeDetail())return;
      const input=field('文件名', 'filename', item.filename);
      const form=el('form',{class:'knowledge-rename'},input);
      const close=()=>{renameArea.hidden=true;renameArea.replaceChildren();ctx.state.dirty=false;rename.focus();};
      const save=el('button',{type:'submit',class:'wb-btn primary'},'保存名称'),cancel=button('取消',close);
      form.append(el('div',{class:'wb-actions'},save,cancel));renameArea.replaceChildren(form);renameArea.hidden=false;
      form.addEventListener('input',()=>ctx.state.dirty=true);
      form.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();close();}});
      form.onsubmit=async event=>{
        event.preventDefault();if(save.disabled)return;save.disabled=cancel.disabled=true;result.replaceChildren();
        try{
          const changed=await wb(kind+'/rename',{data:{path:item.path,revision:item.revision,name:form.elements.filename.value}});
          updateCard(changed.item);
          close();applyFilters(false);result.replaceChildren(message('文件名已修改','success'));
        }catch(error){result.replaceChildren(message(error.message));}
        finally{save.disabled=cancel.disabled=false;}
      };
      input.querySelector('input').focus();input.querySelector('input').select();
    });rename.classList.add('knowledge-rename-trigger');rename.setAttribute('aria-label','重命名'+noun+' '+item.name);rename.title='修改文件名';rename.disabled=!!item.readonly;
    const choices=el('div',{class:'wb-skill-switch',role:'group','aria-label':item.name+' 状态'}),controls=[];
    const paint=()=>{if(skillMode)badge.replaceChildren(tag(item.state));for(const [value,control] of controls){control.classList.toggle('active',value===item.state);control.setAttribute('aria-pressed',String(value===item.state));}};
    if(skillMode)for(const [value,label] of [['enabled','启用'],['disabled','关闭'],['hidden','隐藏']]){
      const control=button(label,async()=>{
        if(value===item.state)return;
        choices.setAttribute('aria-busy','true');controls.forEach(([,b])=>b.disabled=true);
        try{await wb('skills/state',{data:{path:item.path,state:value}});item.state=value;paint();result.replaceChildren(message('已保存，后续上下文按新状态读取','success'));feedback.replaceChildren(message(item.name+'：'+tag(value).textContent,'success'));applyFilters(false);if(card.hidden)search.focus({preventScroll:true});}
        catch(error){result.replaceChildren(message('未能确认保存结果：'+error.message+'。可刷新查看服务器状态。'));}
        finally{choices.removeAttribute('aria-busy');controls.forEach(([,b])=>b.disabled=false);}
      });controls.push([value,control]);choices.append(control);
    }
    const updateCard=updated=>{Object.assign(item,updated);name.textContent=item.name;pathText.textContent=pathText.title=item.path;card.dataset[skillMode?'skillPath':'memoryPath']=item.path;rename.setAttribute('aria-label','重命名'+noun+' '+item.name);preview.textContent=item.summary||item.preview||(skillMode?'空白技能，点击管理填写简介与正文。':'空白记忆，点击查看 / 修改填写内容。');paint();applyFilters(false);};
    card._updateKnowledge=updateCard;
    const manage=button(skillMode?'管理':'查看 / 修改',()=>documentEditor(item,kind,ctx,{onSaved:updateCard}));
    const remove=button('删除',()=>ctx.confirmAction('删除'+noun,'永久删除「'+item.filename+'」？'+(skillMode&&item.source==='public'?'公共技能可能随代码更新恢复；新建的私有技能不受代码更新影响。':'此操作不可撤销。'),async()=>{
      const response=await wb(kind+'/delete',{data:{path:item.path,revision:item.revision,confirm:true}});
      if(sameDocument(ctx,kind,item.path))ctx.closeDetail(true);
      card.remove();const index=items.indexOf(item);if(index>=0)items.splice(index,1);const pair=cards.findIndex(([,node])=>node===card);if(pair>=0)cards.splice(pair,1);applyFilters(false);
      feedback.replaceChildren(message(response.warning||noun+'已删除',response.warning?'error':'success'));
    },null,()=>{ctx.state.dirty=!!page.querySelector('.knowledge-rename-area:not([hidden])');}), 'danger');remove.disabled=!!item.readonly;
    card.append(el('div',{class:'wb-skill-heading'},name,rename,badge),pathText,renameArea,preview,...(skillMode?[choices]:[]),result,
      el('div',{class:'wb-card-footer'},el('span',{class:'wb-note'},skillMode?(item.source==='private'?'私有技能':'公共技能'):'共享记忆'),el('div',{class:'wb-actions'},manage,remove)));
    paint();cards.push([item,card]);grid.append(card);
  }
  applyFilters(false);
}

export async function documentEditor(item,kind,ctx,{isNew=false,onSaved=null}={}){
  const noun=kindName(kind),box=el('div',{},message('读取'+noun+'…','loading'));
  if(!ctx.detail('管理'+noun+' · '+item.name,box))return;
  ctx.state.detailDocument={kind,path:item.path};
  const signal=ctx.state.detailRequest.signal;
  let doc,sourceMode=false,sourceDraft='',parts,sectionsChanged=false,dirty=false;
  let summaryInput,bodyInput,sourceInput,save,status,sourceButton;
  function currentSource(){return kind==='skills'&&sectionsChanged?composeSkillText(summaryInput.value,bodyInput.value):sourceMode?sourceInput.value:sourceDraft;}
  function changed(){dirty=currentSource()!==doc.content;ctx.state.detailDirty=dirty;save.disabled=!dirty||ctx.state.detailSaving;status.textContent=dirty?'有未保存修改':'已保存';}
  function build(data){
    doc=data;sourceDraft=data.content;parts=kind==='skills'?splitSkillText(sourceDraft):{body:sourceDraft};sectionsChanged=false;sourceMode=false;dirty=false;
    ctx.state.detailDirty=false;ctx.state.detailDocument={kind,path:doc.path};
    status=el('span',{class:'wb-note',role:'status'},'已保存');
    const error=el('div',{'aria-live':'polite'}),content=el('div',{class:'knowledge-editor-content'});
    sourceInput=el('textarea',{class:'knowledge-source','aria-label':'源码全文',spellcheck:'false'});sourceInput.value=sourceDraft;
    sourceInput.oninput=()=>{sourceDraft=sourceInput.value;sectionsChanged=false;changed();};
    summaryInput=el('textarea',{'aria-label':'技能简介',rows:5,spellcheck:'false'});summaryInput.value=parts.summary||'';
    bodyInput=el('textarea',{'aria-label':kind==='skills'?'技能正文':'记忆内容',rows:12,spellcheck:'false'});bodyInput.value=parts.body||'';
    const sections=[];
    function section(title,input,allowEmpty){
      const preview=el('div',{class:'knowledge-markdown bubble'});markdown(preview,input.value,allowEmpty?'可留空':'暂无简介');
      const toggle=button('编辑'+title,()=>{input.hidden=!input.hidden;preview.hidden=!input.hidden;toggle.textContent=(input.hidden?'编辑':'预览')+title;if(input.hidden)markdown(preview,input.value,allowEmpty?'可留空':'暂无简介');else input.focus();});
      input.hidden=true;
      input.oninput=()=>{sectionsChanged=true;changed();};
      const section=el('section',{class:'knowledge-section'},el('header',{},el('h3',{},title),allowEmpty?el('span',{class:'wb-note'},'可留空'):null,toggle),preview,input);
      sections.push(section);return section;
    }
    if(kind==='skills')content.append(section('技能简介',summaryInput,false),section('正文',bodyInput,true));
    else{bodyInput.hidden=false;bodyInput.classList.add('knowledge-memory-text');bodyInput.oninput=()=>{sourceDraft=bodyInput.value;changed();};content.append(el('section',{class:'knowledge-section'},el('h3',{},'记忆内容'),bodyInput));}
    sourceButton=button('源码',()=>{
      if(!sourceMode){sourceDraft=currentSource();sourceInput.value=sourceDraft;sectionsChanged=false;content.hidden=true;sourceInput.hidden=false;sourceMode=true;sourceButton.textContent='分块管理';sourceInput.focus();}
      else{sourceDraft=sourceInput.value;parts=splitSkillText(sourceDraft);summaryInput.value=parts.summary;bodyInput.value=parts.body;sectionsChanged=false;content.hidden=false;sourceInput.hidden=true;sourceMode=false;sourceButton.textContent='源码';sections.forEach((section,i)=>markdown(section.querySelector('.knowledge-markdown'),i?bodyInput.value:summaryInput.value,i?'可留空':'暂无简介'));}
      changed();
    });sourceInput.hidden=true;
    save=button('保存修改',async()=>{
      if(ctx.state.detailSaving)return;
      const content=currentSource();ctx.state.detailSaving=true;save.disabled=true;sourceButton.disabled=true;save.textContent='保存中…';const locked=[summaryInput,bodyInput,sourceInput,reload,...sections.flatMap(section=>Array.from(section.querySelectorAll('button')))];locked.forEach(control=>control.disabled=true);error.replaceChildren();
      try{
        const result=await wb(kind+'/update',{data:{path:doc.path,revision:doc.revision,content},signal});
        doc=result.item;sourceDraft=doc.content;sourceInput.value=sourceDraft;sectionsChanged=false;
        const latest=splitSkillText(sourceDraft);summaryInput.value=latest.summary;bodyInput.value=kind==='skills'?latest.body:sourceDraft;
        dirty=false;ctx.state.detailDirty=false;status.textContent='已保存，后续对话生效';
        Object.assign(item,doc);if(onSaved)onSaved(doc);
      }catch(exc){if(exc.name!=='AbortError')error.replaceChildren(message(exc.message),button('重新读取',load));}
      finally{ctx.state.detailSaving=false;sourceButton.disabled=false;save.textContent='保存修改';locked.forEach(control=>control.disabled=false);save.disabled=!dirty;}
    },'primary');save.disabled=true;
    const reload=button('重新读取',load);
    box.replaceChildren(el('div',{class:'knowledge-editor-toolbar'},...(kind==='skills'?[sourceButton,tag(doc.state)]:[]),save,reload,status),error,
      el('p',{class:'wb-skill-path'},doc.path),...(kind==='skills'&&doc.source==='public'?[el('p',{class:'wb-note'},'公共技能的修改可能随代码更新被覆盖；自定义内容建议保存在新建的私有技能中。')]:[]),el('p',{class:'wb-note'},kind==='skills'?'简介会写入技能索引；正文按需读取。源码模式显示完整纯文本，切换视图不会丢失草稿。':'手工记忆为所有对话共享。内容可留空，保存后供后续对话使用。'),content,sourceInput);
    if(isNew){isNew=false;if(kind==='skills'){summaryInput.hidden=false;sections[0].querySelector('.knowledge-markdown').hidden=true;sections[0].querySelector('button').textContent='预览技能简介';summaryInput.focus();}else bodyInput.focus();}
  }
  async function load(){
    if(ctx.state.detailSaving)return;
    if(dirty&&!confirm('重新读取会丢弃未保存修改，是否继续？'))return;
    try{const data=await wb(kind+query({path:item.path}),{signal});if(!signal.aborted)build(data);}
    catch(error){if(error.name!=='AbortError'){if(!doc)box.replaceChildren(message(error.message),button('重新读取',load));else box.prepend(message(error.message));}}
  }
  await load();
}
