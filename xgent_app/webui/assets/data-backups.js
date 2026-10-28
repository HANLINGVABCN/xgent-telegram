import {el,button,field,select,check,wb,message,bytes,requestId} from './components.js';

const FORMAT_DEFAULTS=[
  {id:'zip',label:'ZIP · 通用',description:'通用兼容，均衡压缩'},
  {id:'tar.gz',label:'TAR.GZ · 最快模式',description:'低压缩级别，优先速度'},
  {id:'tar.xz',label:'TAR.XZ · 最小模式',description:'更高压缩率，优先体积'}
];
const PHASES={pending:'等待开始',snapshotting:'正在创建数据库快照',packing:'正在压缩',retrying:'文件发生变化，正在重试',verifying:'正在扫描并校验内容',verified:'扫描与校验通过',backing_up:'正在保存导入前自动备份',activating:'正在替换数据并清除旧内容',restarting:'正在切换并重启',starting:'正在启动服务',completed:'已完成',cancelled:'已取消扫描'};
const store=(key,value)=>{try{if(value)sessionStorage.setItem(key,value);else sessionStorage.removeItem(key);}catch{}};
const stored=key=>{try{return sessionStorage.getItem(key);}catch{return null;}};
const seconds=value=>value==null?'—':value<60?Number(value).toFixed(1)+' 秒':Math.floor(value/60)+' 分 '+Math.round(value%60)+' 秒';

function fileDetails(category){
  const rows=(category.files||[]).map(file=>el('li',{},el('code',{},file.name),el('span',{},bytes(file.size))));
  return el('div',{class:'wb-backup-file-details'},rows.length?el('ul',{},rows):el('p',{},'此类型没有可导出的文件。'),category.files_truncated?el('p',{class:'wb-note'},'仅展示前 200 个文件；压缩和校验仍处理全部文件。'):null);
}
function showProgress(container,job){
  container.replaceChildren(el('p',{},PHASES[job.phase]||job.phase||job.state));
  if(job.total_bytes)container.append(el('progress',{value:Math.min(job.completed_bytes||0,job.total_bytes),max:job.total_bytes}),el('small',{},bytes(job.completed_bytes||0)+' / '+bytes(job.total_bytes)));
  if(job.current_file)container.append(el('small',{class:'wb-file-path'},job.current_file));
}

export async function dataBackupCards({page,card,state={},dirtyForm=()=>{}}){
  const [initialEstimate,initialRules,initialCatalog]=await Promise.all([wb('data/estimate'),wb('data/backup-settings'),wb('data/backups')]);
  let estimate=initialEstimate,rules=initialRules,catalog=initialCatalog;
  const categories=estimate.categories||[];
  const chosen=new Set(rules.categories||estimate.recommended_categories||estimate.selected_categories||categories.filter(item=>item.selected).map(item=>item.id));
  let format=rules.archive_format||'zip',exportBusy=false,activating=false;
  let estimateSerial=0,estimateTimer=null,estimateAbort=null,selectionSerial=0;
  let exportTimer=null,importTimer=null,importId=null,selectedName=null,currentSource='';
  const form=el('form',{class:'wb-backup-config-form'}),formFields=el('fieldset',{class:'wb-backup-config-fields'});
  const feedback=el('div',{class:'wb-backup-rule-feedback'}),exportProgress=el('div',{class:'wb-data-progress',role:'status','aria-live':'polite'});
  const totalText=el('strong',{class:'wb-backup-total'}),forecast=el('span',{class:'wb-backup-forecast'});
  const body=el('tbody'),inputs=new Map(),formatLabels=new Map();
  const allSize=categories.reduce((total,item)=>total+(item.size||0),0);
  for(const item of categories){
    const input=el('input',{type:'checkbox',name:'category_'+item.id,checked:chosen.has(item.id),'data-category':item.id,'aria-label':'导出 '+item.label});inputs.set(item.id,input);
    input.onchange=()=>{if(input.checked){chosen.add(item.id);resolveConflicts(item.id);}else chosen.delete(item.id);changedSelection();};
    const info=el('details',{class:'wb-backup-category'},el('summary',{},el('span',{},item.label),item.sensitive?el('span',{class:'wb-backup-sensitive'},'含敏感信息'):null),item.description?el('p',{class:'wb-note'},item.description):null,el('small',{class:'wb-file-path'},(item.paths||[]).join(' · ')||(item.part?'独立分类记录 · 数据库 / 环境变量':'无文件')),fileDetails(item));
    const meter=el('span',{class:'wb-backup-size-fill'});meter.style.width=(allSize?Math.min(100,(item.size||0)/allSize*100):0)+'%';
    body.append(el('tr',{'data-export-category':item.id},el('td',{},input),el('td',{},info),el('td',{},el('strong',{},(item.size_is_estimate?'约 ':'')+bytes(item.size)),el('span',{class:'wb-backup-size-meter','aria-hidden':'true'},meter)),el('td',{},String(item.file_count||0),item.item_count!=null?el('small',{},item.item_count+' 条记录'):null)));
  }
  const setChoices=ids=>{chosen.clear();ids.forEach(id=>chosen.add(id));for(const [id,input] of inputs)input.checked=chosen.has(id);form.dispatchEvent(new Event('input',{bubbles:true}));changedSelection();};
  const contents=el('section',{class:'wb-backup-contents'},el('div',{class:'wb-backup-section-head'},el('h4',{},'全部导出类型 · '+categories.length+' 类'),el('div',{class:'wb-actions'},button('全选',()=>setChoices(categories.filter(item=>!item.advanced).map(item=>item.id))),button('全不选',()=>setChoices([])),button('默认选择',()=>setChoices(estimate.recommended_categories||categories.filter(item=>!item.advanced&&item.id!=='command_outputs').map(item=>item.id))))),el('p',{class:'wb-note'},'全部类型均列出，空类型也显示。密钥、AI 配置、对话记忆可独立导出；全选使用独立分类，不重复勾选完整文件。源码、公共技能和缓存不导出。'),el('div',{class:'wb-table-wrap wb-backup-content-table'},el('table',{class:'wb-table'},el('thead',{},el('tr',{},el('th',{},'选择'),el('th',{},'数据类型 / 文件'),el('th',{},'空间占用'),el('th',{},'文件数'))),body)));

  const enabled=check('开启自动压缩','enabled',rules.enabled);
  const presets=[{value:'30m',label:'每 30 分钟'},{value:'1h',label:'每小时'},{value:'6h',label:'每 6 小时'},{value:'12h',label:'每 12 小时'},{value:'1d',label:'每天'},{value:'7d',label:'每周'},{value:'custom',label:'自定义间隔'}];
  const frequencyValue=rules.frequency||'1d';
  const frequency=select('压缩频率','frequency_preset',presets,presets.some(item=>item.value===frequencyValue)?frequencyValue:'custom');
  const customFrequency=field('自定义间隔','frequency',frequencyValue,'text','支持 30m、6h、1d；最低 1 分钟。');
  const mode=select('保存方式','mode',[{value:'accumulate',label:'累计保留，自动清理最老压缩包'},{value:'overwrite',label:'覆盖同格式上一份压缩包'}],rules.mode||'accumulate');
  const limit=field('最多保留份数','max_backups',rules.max_backups||7,'number');limit.querySelector('input').min='1';limit.querySelector('input').step='1';
  const directory=field('服务器压缩包目录','backup_directory',rules.backup_directory,'text','项目目录之外的绝对路径。');
  frequency.querySelector('select').setAttribute('aria-label','压缩频率');
  customFrequency.querySelector('input').setAttribute('aria-label','自定义间隔');
  mode.querySelector('select').setAttribute('aria-label','保存方式');
  limit.querySelector('input').setAttribute('aria-label','最多保留份数');
  const directoryInput=directory.querySelector('input');directoryInput.setAttribute('aria-label','服务器压缩包目录');
  const syncFrequency=()=>{customFrequency.hidden=frequency.querySelector('select').value!=='custom';};frequency.onchange=syncFrequency;syncFrequency();
  const syncMode=()=>{limit.querySelector('input').disabled=exportBusy||mode.querySelector('select').value==='overwrite';};mode.onchange=syncMode;syncMode();
  const schedule=el('section',{class:'wb-backup-schedule'},el('h4',{},'压缩机制'),enabled,frequency,customFrequency,mode,limit,directory,button('使用默认目录',()=>{directoryInput.value=rules.default_backup_directory;form.dispatchEvent(new Event('input',{bubbles:true}));}),el('p',{class:'wb-note'},'自动压缩使用下方相同的内容与格式；配置与安装脚本共享，保存后约 15 秒内生效。'));
  const formatBox=el('div',{class:'wb-backup-formats',role:'radiogroup','aria-label':'压缩格式'});
  for(const spec of FORMAT_DEFAULTS){
    const input=el('input',{type:'radio',name:'archive_format',value:spec.id,checked:format===spec.id});
    const prediction=el('strong',{class:'wb-backup-format-estimate'},'正在估算…');
    const label=el('label',{class:'wb-backup-format'+(format===spec.id?' is-selected':'')},el('span',{class:'wb-backup-format-title'},input,el('strong',{},spec.label)),el('small',{},spec.description),prediction);
    input.onchange=()=>{format=spec.id;for(const [id,value] of formatLabels)value.label.classList.toggle('is-selected',id===format);updateSummary();};
    formatLabels.set(spec.id,{label,prediction,input});formatBox.append(label);
  }
  const formatSection=el('section',{class:'wb-backup-format-section'},el('h4',{},'压缩方法'),formatBox,el('p',{class:'wb-note'},estimate.estimate_note||'时间与压缩大小为抽样估算，不是保证值。'));
  formFields.append(el('div',{class:'wb-backup-config-grid'},contents,schedule),formatSection);
  const saveButton=el('button',{type:'submit',class:'wb-btn'},'保存配置');
  const exportButton=button('立即压缩',startExport,'primary');
  form.append(formFields,el('div',{class:'wb-backup-config-footer'},el('div',{class:'wb-backup-selection-summary',role:'status','aria-live':'polite'},totalText,forecast),el('div',{class:'wb-actions'},saveButton,exportButton)),el('p',{class:'wb-note'},'立即压缩会先保存当前配置；后台压缩，不停服、不重启。导入则需要另行确认。'),feedback,exportProgress);
  dirtyForm(form);
  form.onsubmit=async event=>{event.preventDefault();saveButton.disabled=true;try{await saveRules();feedback.replaceChildren(message('压缩配置已保存。','success'));}catch(error){feedback.replaceChildren(message(error.message));}finally{updateSummary();}};

  const listBox=el('div',{class:'wb-backup-list'}),listFeedback=el('div');
  const importProgress=el('div',{class:'wb-data-progress',role:'status','aria-live':'polite'}),previewBox=el('div',{class:'wb-import-preview'});
  const sourceText=el('p',{class:'wb-import-source'});
  const file=el('input',{type:'file',accept:'.zip,.tar.gz,.tar.xz,application/zip,application/gzip,application/x-xz','aria-label':'上传压缩包以扫描内容'});
  const dropzone=el('div',{class:'wb-backup-dropzone'},el('strong',{},'上传压缩包'),el('p',{},'拖放 ZIP、TAR.GZ、TAR.XZ，或选择文件。选择后立即上传并扫描，不会直接导入。'),file);
  file.onchange=()=>{if(file.files[0])uploadFile(file.files[0]);};
  dropzone.ondragover=event=>{event.preventDefault();if(!activating)dropzone.classList.add('dragover');};
  dropzone.ondragleave=()=>dropzone.classList.remove('dragover');
  dropzone.ondrop=event=>{event.preventDefault();dropzone.classList.remove('dragover');if(activating)return;const files=[...event.dataTransfer.files];if(files.length!==1){importProgress.replaceChildren(message('请一次上传一个压缩包。'));return;}uploadFile(files[0]);};
  const listCard=card('压缩包',el('p',{},'点击名称，右侧立即扫描内容；勾选框仅用于批量删除。'),el('div',{class:'wb-actions'},button('上传压缩包',()=>file.click()),button('刷新列表',async()=>{try{await refreshCatalog();}catch(error){listFeedback.replaceChildren(message(error.message));}})),listFeedback,listBox);
  listCard.classList.add('wb-backup-catalog-card');
  const importCard=card('导入',dropzone,sourceText,importProgress,previewBox);importCard.classList.add('wb-backup-import-card');
  const split=el('div',{class:'wb-backup-split'},listCard,importCard);
  const configCard=card('压缩配置',el('p',{},'选择导出内容、压缩频率和格式。预计时间与大小会随所选内容更新。'),form);configCard.classList.add('wb-backup-config-card');
  previewBox.replaceChildren(el('div',{class:'wb-empty'},el('h4',{},'选择一个压缩包'),el('p',{},'点击左侧名称或上传文件，扫描通过后显示包含内容与覆盖范围。')));

  function updateSummary(){
    const selected=categories.filter(item=>chosen.has(item.id));
    totalText.textContent='已选 '+selected.length+' 类 · '+selected.reduce((sum,item)=>sum+(item.file_count||0),0)+' 个文件 · 原始 '+bytes(selected.reduce((sum,item)=>sum+(item.size||0),0));
    const prediction=(estimate.formats||[]).find(item=>item.id===format);
    forecast.textContent=chosen.size?(prediction?'预计 '+seconds(prediction.estimated_seconds)+' · 压缩后约 '+bytes(prediction.estimated_size):'正在更新估算…'):'请选择至少一种数据类型';
    saveButton.disabled=exportBusy||!chosen.size;exportButton.disabled=exportBusy||!chosen.size;
    for(const spec of FORMAT_DEFAULTS){const value=(estimate.formats||[]).find(item=>item.id===spec.id);formatLabels.get(spec.id).prediction.textContent=!chosen.size?'请选择内容':value?'预计 '+seconds(value.estimated_seconds)+' · 约 '+bytes(value.estimated_size):'正在估算…';}
  }
  function resolveConflicts(key){
    for(const item of categories){if(item.id!==key&&((item.conflicts||[]).includes(key)||(categories.find(value=>value.id===key)?.conflicts||[]).includes(item.id))){chosen.delete(item.id);inputs.get(item.id).checked=false;}}
  }
  function changedSelection(){estimate={...estimate,formats:[]};updateSummary();clearTimeout(estimateTimer);estimateTimer=setTimeout(refreshEstimate,180);}
  async function refreshEstimate(){
    const serial=++estimateSerial;estimateAbort?.abort();
    if(!chosen.size)return;
    estimateAbort=new AbortController();
    try{const value=await wb('data/estimate?categories='+encodeURIComponent([...chosen].join(',')),{signal:estimateAbort.signal});if(serial!==estimateSerial)return;estimate=value;updateSummary();}
    catch(error){if(error.name!=='AbortError'&&serial===estimateSerial){feedback.replaceChildren(message('估算未完成：'+error.message));for(const value of formatLabels.values())value.prediction.textContent='估算不可用';forecast.textContent='估算不可用，仍可压缩';}}
  }
  function settingsData(){
    return {backup_directory:directoryInput.value.trim(),enabled:enabled.querySelector('input').checked,
      frequency:frequency.querySelector('select').value==='custom'?customFrequency.querySelector('input').value.trim():frequency.querySelector('select').value,
      mode:mode.querySelector('select').value,max_backups:Number(limit.querySelector('input').value),categories:[...chosen],archive_format:format,include_command_outputs:chosen.has('command_outputs')};
  }
  async function saveRules(){
    if(!chosen.size)throw new Error('请至少选择一种数据类型。');
    rules=await wb('data/backup-settings',{data:settingsData()});directoryInput.value=rules.backup_directory;state.dirty=false;await refreshCatalog();return rules;
  }
  function setExportBusy(value){exportBusy=value;formFields.disabled=value;syncMode();updateSummary();}
  async function startExport(){
    if(exportBusy||!chosen.size)return;
    if(categories.some(item=>chosen.has(item.id)&&item.sensitive)&&!confirm('所选内容可能包含 .env 原文、Bot/API 密钥或私有配置。确认压缩并妥善保管？'))return;
    setExportBusy(true);feedback.replaceChildren();exportProgress.replaceChildren(message('正在保存配置并开始压缩…','loading'));
    try{await saveRules();const job=await wb('data/export',{data:{request_id:requestId(),categories:[...chosen],archive_format:format,include_command_outputs:chosen.has('command_outputs'),confirm_secrets:true}});store('xgent-data-export-job',job.id);pollExport(job.id);}
    catch(error){exportProgress.replaceChildren(message(error.message));setExportBusy(false);}
  }
  async function pollExport(id){
    clearTimeout(exportTimer);
    try{const job=await wb('data/status?id='+encodeURIComponent(id));showProgress(exportProgress,job);
      if(job.state==='completed'){store('xgent-data-export-job',null);setExportBusy(false);exportProgress.append(message('压缩完成 · '+bytes(job.result?.size),'success'),el('a',{class:'wb-btn primary',href:job.download_url,download:''},'下载压缩包'));await refreshCatalog();return;}
      if(job.state==='failed'){store('xgent-data-export-job',null);setExportBusy(false);exportProgress.append(message(job.error||'压缩失败，旧压缩包保留。'));return;}
      exportTimer=setTimeout(()=>pollExport(id),800);
    }catch(error){exportProgress.replaceChildren(message('状态读取失败：'+error.message),button('重试读取状态',()=>pollExport(id)));setExportBusy(false);}
  }
  function renderCatalog(next){
    catalog=next;const entries=el('div',{class:'wb-backup-entries'});
    for(const item of catalog.items||[]){
      const box=el('input',{type:'checkbox','data-backup-name':item.name,'aria-label':'删除选择 '+item.name});
      const choose=button('',()=>selectStored(item.name));choose.classList.add('wb-backup-entry-select');
      choose.append(el('strong',{},item.name),el('small',{},item.label+' · '+bytes(item.size)+' · '+new Date(item.created_at*1000).toLocaleString('zh-CN',{hour12:false})),el('small',{},(item.file_count==null?'文件数未知':item.file_count+' 个文件')+' · '+(item.archive_format||'ZIP').toUpperCase()),el('span',{class:'wb-backup-entry-types'},(item.categories||[]).slice(0,4).map(category=>el('span',{class:'wb-tag'},category.label)),(item.categories||[]).length>4?el('span',{class:'wb-tag'},'+'+(item.categories.length-4)+' 类'):null));
      choose.setAttribute('aria-pressed',String(selectedName===item.name));
      entries.append(el('div',{class:'wb-backup-entry'+(selectedName===item.name?' is-selected':''),'data-archive-name':item.name},box,choose,el('a',{class:'wb-btn',href:'/api/workbench/data/backup-download?name='+encodeURIComponent(item.name),download:''},'下载')));
    }
    const remove=button('删除选中压缩包',async()=>{const names=[...listBox.querySelectorAll('[data-backup-name]:checked')].map(input=>input.dataset.backupName);if(!names.length){listFeedback.replaceChildren(message('请先勾选需要删除的压缩包。'));return;}if(!confirm('永久删除 '+names.length+' 个压缩包？此操作不可撤销。'))return;remove.disabled=true;try{if(names.includes(selectedName))await beginSelection('');renderCatalog(await wb('data/backups/delete',{data:{names,confirm:true}}));listFeedback.replaceChildren(message('压缩包已删除。','success'));}catch(error){listFeedback.replaceChildren(message(error.message));}finally{remove.disabled=false;}},'danger');
    listBox.replaceChildren(el('p',{class:'wb-file-path'},'目录：',el('code',{},catalog.directory)),entries.childElementCount?entries:el('div',{class:'wb-empty'},el('h4',{},'还没有压缩包'),el('p',{},'立即压缩、自动压缩及导入前自动备份会显示在这里。')),el('div',{class:'wb-actions'},remove));
  }
  async function refreshCatalog(){renderCatalog(await wb('data/backups'));}
  async function cancelScan(id){if(!id)return;try{await wb('data/import/cancel',{data:{id}});}catch{}}
  async function beginSelection(source,name=null){
    if(activating)return null;
    const serial=++selectionSerial,previous=importId;importId=null;clearTimeout(importTimer);store('xgent-data-import-job',null);selectedName=name;currentSource=source;sourceText.textContent=source?'当前压缩包：'+source:'';
    for(const row of listBox.querySelectorAll('[data-archive-name]')){const selected=row.dataset.archiveName===name;row.classList.toggle('is-selected',selected);row.querySelector('.wb-backup-entry-select').setAttribute('aria-pressed',String(selected));}
    previewBox.replaceChildren();importProgress.replaceChildren(source?message('正在扫描 '+source+'…','loading'):message('请选择压缩包。','loading'));await cancelScan(previous);return serial;
  }
  async function selectStored(name){
    const serial=await beginSelection(name,name);if(serial==null)return;
    try{const job=await wb('data/import-stored',{data:{name,request_id:requestId()}});if(serial!==selectionSerial){await cancelScan(job.id);return;}importId=job.id;store('xgent-data-import-job',job.id);pollImport(job.id,serial);}
    catch(error){if(serial===selectionSerial)importProgress.replaceChildren(message(error.message));}
  }
  async function uploadFile(uploaded){
    const serial=await beginSelection(uploaded.name);if(serial==null)return;
    if(!/\.(zip|tar\.gz|tar\.xz)$/i.test(uploaded.name)){importProgress.replaceChildren(message('请选择 ZIP、TAR.GZ 或 TAR.XZ 压缩包。'));return;}
    const data=new FormData();data.append('file',uploaded);data.append('text','scan-instance-backup');data.append('idempotency_key',requestId());
    const xhr=new XMLHttpRequest();xhr.open('POST','/api/workbench/data/import');
    xhr.upload.onprogress=event=>{if(serial!==selectionSerial||!event.lengthComputable)return;importProgress.replaceChildren(el('p',{},'正在上传 · '+Math.floor(event.loaded/event.total*100)+'%'),el('progress',{max:event.total,value:event.loaded}));};
    xhr.onload=async()=>{let result={};try{result=JSON.parse(xhr.responseText);}catch{}if(serial!==selectionSerial){if(result.id)await cancelScan(result.id);return;}if(xhr.status!==200){importProgress.replaceChildren(message(result.error||'上传失败；当前数据未修改。'));return;}importId=result.id;store('xgent-data-import-job',result.id);pollImport(result.id,serial);};
    xhr.onerror=()=>{if(serial===selectionSerial)importProgress.replaceChildren(message('上传连接失败；当前数据未修改。'));};xhr.send(data);
  }
  function renderPreview(job){
    const preview=job.preview;if(!preview){previewBox.replaceChildren(message('此任务没有内容预览，请重新选择压缩包。'));return;}
    currentSource=currentSource||job.source_name;sourceText.textContent='当前压缩包：'+currentSource;
    const rows=(preview.categories||[]).filter(item=>item.available!==false&&item.included!==false);
    const selected=new Set(preview.recommended_categories||rows.filter(item=>!item.advanced).map(item=>item.id));
    const choiceInputs=new Map(),selectionSummary=el('strong',{class:'wb-import-selected-summary'}),warningText=el('p');
    const warning=el('div',{class:'wb-backup-overwrite-warning',role:'note'},el('strong',{},'只覆盖勾选的部分；这些部分中的旧内容会删除。未勾选的全部保留。'),warningText,el('p',{},'密钥、AI 配置、应用设置、对话记忆分别恢复，不会连带覆盖其他数据库内容。切换前自动备份；失败会尝试回滚。'));
    const groups=el('div',{class:'wb-import-content-groups'});
    for(const item of rows){
      const input=el('input',{type:'checkbox',checked:selected.has(item.id),'data-import-category':item.id,'aria-label':'导入 '+item.label});choiceInputs.set(item.id,input);
      input.onchange=()=>{if(input.checked){selected.add(item.id);for(const other of rows){if(other.id!==item.id&&((item.conflicts||[]).includes(other.id)||(other.conflicts||[]).includes(item.id))){selected.delete(other.id);choiceInputs.get(other.id).checked=false;}}}else selected.delete(item.id);syncImport();};
      const details=el('details',{class:'wb-import-content-group'},el('summary',{},el('strong',{},item.label),el('span',{},item.file_count+' 个文件 · '+bytes(item.size)+(item.item_count!=null?' · '+item.item_count+' 条记录':''))),item.description?el('p',{class:'wb-note'},item.description):null,el('p',{class:'wb-import-replacement'},'当前 '+(item.current_item_count!=null?item.current_item_count+' 条记录':item.current_file_count+' 个文件')+' / '+bytes(item.current_size)+' → 导入 '+item.file_count+' 个文件 / '+bytes(item.size)),el('p',{class:'wb-file-path'},'覆盖范围：'+(item.replace_paths||item.paths||[]).join(' · ')),item.entry_names?.length?el('p',{class:'wb-note'},'条目名称（不显示密钥值）：'+item.entry_names.join('、')):null,fileDetails(item));
      groups.append(el('div',{class:'wb-import-select-row','data-import-row':item.id},input,details));
    }
    const activate=button('确认覆盖并导入',async()=>{
      if(activating||importId!==job.id)return;
      if(!selected.size)return;
      const labels=rows.filter(item=>selected.has(item.id)).map(item=>item.label).join('、');
      if(!confirm('将停止服务，并覆盖 '+labels+'。这些类型中的旧内容会删除，不会合并。切换前会自动备份旧数据，随后重启服务。确认导入？'))return;
      activating=true;activate.disabled=true;file.disabled=true;for(const input of choiceInputs.values())input.disabled=true;
      try{await wb('data/activate',{data:{id:job.id,categories:[...selected],confirm_replace:true}});previewBox.replaceChildren();pollImport(job.id,selectionSerial);}
      catch(error){activating=false;file.disabled=false;for(const input of choiceInputs.values())input.disabled=false;importProgress.replaceChildren(message(error.message));syncImport();}
    },'danger');
    function syncImport(){const included=rows.filter(item=>selected.has(item.id));selectionSummary.textContent='已选导入 '+included.length+' 部分 · '+bytes(included.reduce((sum,item)=>sum+(item.size||0),0));warningText.textContent=included.length?'将覆盖：'+included.map(item=>item.label).join('、'):'尚未选择导入部分；当前数据不会修改。';activate.disabled=!selected.size;for(const row of groups.querySelectorAll('[data-import-row]'))row.classList.toggle('is-selected',selected.has(row.dataset.importRow));}
    const chooseAll=()=>{selected.clear();rows.filter(item=>!item.advanced).forEach(item=>selected.add(item.id));for(const [key,input] of choiceInputs)input.checked=selected.has(key);syncImport();};
    const chooseNone=()=>{selected.clear();for(const input of choiceInputs.values())input.checked=false;syncImport();};
    previewBox.replaceChildren(el('div',{class:'wb-import-summary'},el('strong',{},'可导入 '+rows.length+' 部分 · 压缩包 '+bytes(preview.size)),el('span',{class:'wb-tag'},(preview.archive_format||'ZIP').toUpperCase())),warning,el('div',{class:'wb-backup-section-head'},el('h4',{},'选择需要导入的部分'),el('div',{class:'wb-actions'},button('全选可导入部分',chooseAll),button('全不选导入部分',chooseNone))),groups,selectionSummary,el('p',{class:'wb-note'},'只有勾选部分会覆盖；完整数据库与独立数据库分类互斥。导入同时更新路径映射与恢复记录。'),activate);syncImport();
  }
  async function pollImport(id,serial){
    clearTimeout(importTimer);if(serial!==selectionSerial)return;
    try{const job=await wb('data/status?id='+encodeURIComponent(id));if(serial!==selectionSerial)return;showProgress(importProgress,job);
      if(job.state==='ready'){renderPreview(job);return;}
      if(job.state==='failed'){activating=false;file.disabled=false;previewBox.replaceChildren();importProgress.append(message(job.error||'扫描或导入失败，原数据仍保留。'));return;}
      if(job.state==='cancelled'){store('xgent-data-import-job',null);return;}
      if(job.state==='completed'){activating=false;file.disabled=false;store('xgent-data-import-job',null);previewBox.replaceChildren(message('导入完成，所覆盖的旧内容已清理。','success'));await refreshCatalog();return;}
      importTimer=setTimeout(()=>pollImport(id,serial),800);
    }catch(error){importProgress.replaceChildren(message('状态读取失败：'+error.message),button('重试读取状态',()=>pollImport(id,serial)));if(activating)importTimer=setTimeout(()=>pollImport(id,serial),3000);}
  }
  renderCatalog(catalog);updateSummary();
  if(JSON.stringify([...chosen].sort())!==JSON.stringify((initialEstimate.selected_categories||[]).slice().sort()))changedSelection();
  const pendingExport=stored('xgent-data-export-job');if(pendingExport){setExportBusy(true);setTimeout(()=>pollExport(pendingExport),0);}
  const pendingImport=stored('xgent-data-import-job');if(pendingImport){importId=pendingImport;setTimeout(()=>pollImport(pendingImport,selectionSerial),0);}
  const legacy=stored('xgent-data-job');if(legacy){store('xgent-data-job',null);wb('data/status?id='+encodeURIComponent(legacy)).then(job=>{if(job.kind==='export'){store('xgent-data-export-job',legacy);setExportBusy(true);pollExport(legacy);}else{importId=legacy;store('xgent-data-import-job',legacy);pollImport(legacy,selectionSerial);}}).catch(()=>{});}
  return [configCard,split];
}
