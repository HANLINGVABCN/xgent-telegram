import {el,button,field,select,check,wb,message,tag,bytes,requestId} from './components.js';

export async function settingsView({logout,boot,health,state,render,dialog,closeDialog,submit,dirtyForm,confirmAction,refreshBootstrap,setTheme,filter,heading,card,actions}) {
  const settings=boot.settings;
  const page=el('div',{},heading('设置','业务设置与 Telegram、CLI 共享。字号保存到服务端并跨网页设备同步；主题与动效仅影响当前浏览器。',[button('退出登录',logout,'danger')]));
  const sections=[['appearance','外观'],['conversation','对话与记忆'],['agent','Agent'],['web','Web 与安全'],['data','数据与备份'],['health','系统状态']];
  const active=filter('section')||'appearance';
  page.append(el('div',{class:'wb-tabs',role:'tablist'},sections.map(([id,title])=>el('button',{role:'tab','aria-selected':id===active,class:id===active?'active':'',onclick:()=>{if(state.dirty&&!confirm('尚有未保存设置，放弃修改？'))return;state.dirty=false;if(active==='appearance'&&id!==active)window.XGentAppearance?.cancelPreview();filter('section',id);render();}},title))));
  const grid=el('div',{class:'wb-settings-grid'});
  page.classList.add('wb-preferences-page');
  if(active==='data'){
    const estimate=await wb('data/estimate');
    const outputs=check('包含历史命令完整回执 command_outputs/（默认不选）','include_command_outputs');
    const size=el('p',{class:'wb-note'},'核心数据：'+bytes(estimate.size)+' · '+estimate.file_count+' 个文件');
    const progress=el('div',{class:'wb-data-progress',role:'status'});
    const warning=el('p',{class:'wb-note'},'不包含命令回执不会影响数据库中的历史聊天、调用摘要与上下文，但无法从此备份恢复历史命令的完整原始终端输出。勾选将额外加入约 '+bytes(estimate.command_outputs_size)+'，显著增加打包、下载和同步时间。');
    outputs.onchange=async()=>{try{const value=await wb('data/estimate?include_command_outputs='+outputs.querySelector('input').checked);size.textContent='估算：'+bytes(value.size)+' · '+value.file_count+' 个文件';}catch(error){progress.replaceChildren(message(error.message));}};
    let polling=null;
    async function poll(id){
      clearTimeout(polling);
      try{
        const job=await wb('data/status?id='+encodeURIComponent(id));
        if(!page.isConnected)return;
        progress.replaceChildren(el('p',{},({packing:'正在打包',verifying:'正在校验',pending:'等待开始',verified:'校验通过',completed:'已完成',restarting:'正在切换并重启'})[job.phase]||job.phase||job.state));
        if(job.total_bytes)progress.append(el('progress',{value:job.completed_bytes||0,max:job.total_bytes}),el('small',{},bytes(job.completed_bytes)+' / '+bytes(job.total_bytes)));
        if(job.state==='failed'){progress.append(message(job.error||'操作未完成，原数据仍保留'));return;}
        if(job.state==='completed'&&job.download_url){progress.append(el('a',{class:'wb-btn primary',href:job.download_url,download:''},'下载含密钥备份'),el('small',{class:'wb-file-path'},job.result?.path||''));sessionStorage.removeItem('xgent-data-job');return;}
        if(job.state==='ready'){
          progress.append(el('p',{},'SHA-256 和 SQLite 校验通过，共 '+job.file_count+' 个文件。'),button('确认切换整套数据并重启',async()=>{
            if(!confirm('将停止服务并切换整套数据，当前数据目录会保留用于回退。恢复后的 Web 密码、端口和 Bot 配置以备份为准。确定继续？'))return;
            try{await wb('data/activate',{data:{id:job.id,confirm_replace:true}});progress.replaceChildren(message('正在切换并重启，请稍后刷新并使用备份中的密码登录。','note'));}
            catch(error){progress.append(message(error.message));}
          },'danger'));return;
        }
        if(job.state==='activating'){progress.append(message('服务正在重启；重新连接后可继续查看状态。','note'));return;}
        polling=setTimeout(()=>poll(id),800);
      }catch(error){if(page.isConnected)progress.replaceChildren(message(error.message),button('重试查询',()=>poll(id)));}
    }
    const exportButton=button('创建核心数据备份',async()=>{
      if(!confirm('此备份包含 .env 原文、Bot/API 密钥及完整数据库。请仅保存到可信位置，不要公开上传。确认创建？'))return;
      exportButton.disabled=true;
      try{const job=await wb('data/export',{data:{request_id:requestId(),confirm_secrets:true,include_command_outputs:outputs.querySelector('input').checked}});sessionStorage.setItem('xgent-data-job',job.id);await poll(job.id);}
      catch(error){progress.replaceChildren(message(error.message));}finally{exportButton.disabled=false;}
    },'primary');
    const file=el('input',{type:'file',accept:'.zip,application/zip','aria-label':'选择实例备份 ZIP'});
    const importButton=button('上传并校验备份',async()=>{
      if(!file.files[0]){progress.replaceChildren(message('请选择实例备份 ZIP'));return;}
      if(!confirm('备份可能含密钥。先上传和校验，不会立即替换当前数据；校验后还需确认切换。继续？'))return;
      importButton.disabled=true;
      const data=new FormData();data.append('file',file.files[0]);data.append('text','confirm-instance-restore');data.append('idempotency_key',requestId());
      const xhr=new XMLHttpRequest();xhr.open('POST','/api/workbench/data/import');
      xhr.upload.onprogress=event=>{if(event.lengthComputable)progress.replaceChildren(el('progress',{max:event.total,value:event.loaded}),el('span',{},'上传 '+Math.floor(event.loaded/event.total*100)+'%'+(event.loaded===event.total?' · 正在接收确认':'')));};
      xhr.onload=()=>{importButton.disabled=false;let body={};try{body=JSON.parse(xhr.responseText);}catch{}if(xhr.status!==200){progress.replaceChildren(message(body.error||'备份上传失败'));return;}sessionStorage.setItem('xgent-data-job',body.id);poll(body.id);};
      xhr.onerror=()=>{importButton.disabled=false;progress.replaceChildren(message('上传连接失败，请重试；当前数据未修改。'));};xhr.send(data);
    });
    grid.append(card('标准化实例备份',el('p',{},'包含数据库、.env 原文、长期记忆、私有技能、附件原件、附件版本、生成媒体、对话导出与服务器资产；不含日志、虚拟环境或缓存。'),size,outputs,warning,exportButton),card('整套导入恢复',el('p',{},'只恢复到代码外的独立数据目录。先校验文件哈希和 SQLite，再确认切换；原目录保留为回退目录，不与当前实例合并。'),file,importButton),card('处理状态',progress));
    const pending=sessionStorage.getItem('xgent-data-job');if(pending)setTimeout(()=>poll(pending),0);
  }
  if(active==='appearance'){
    const appearance=card('文字字号',el('p',{},'消息、界面和网页终端字号都在这里调整。预览只影响本页，保存后写入服务端配置并同步到其他网页设备。'));
    const theme=select('主题','theme',[{value:'system',label:'跟随系统'},{value:'light',label:'浅色'},{value:'dark',label:'深色'}],localStorage.getItem('xgent-theme')||'system');theme.querySelector('select').onchange=e=>setTheme(e.target.value);
    const reduce=check('减少界面动效','reduce',localStorage.getItem('xgent-reduce-motion')==='1');reduce.onchange=e=>{localStorage.setItem('xgent-reduce-motion',e.target.checked?'1':'0');document.body.classList.toggle('wb-reduce-motion',e.target.checked);};
    const fonts=window.XGentAppearance, fontForm=el('form',{class:'font-settings'});
    const fontKeys=[['web_message_font_size','消息字号'],['web_ui_font_size','界面字号'],['web_terminal_font_size','网页终端字号']];
    for(const [key,label] of fontKeys){const control=field(label+'（px）',key,fonts.state.values[key],'number');const input=control.querySelector('input');[input.min,input.max]=fonts.limits[key];input.step='1';const slider=el('input',{type:'range',min:input.min,max:input.max,step:'1','aria-label':label+'滑块','data-font-key':key});slider.value=input.value;slider.oninput=()=>input.value=slider.value;input.oninput=()=>{if(input.checkValidity())slider.value=input.value;};fontForm.append(el('div',{class:'font-control'},control,slider));}
    const preview=el('div',{class:'font-preview'},'消息预览：中文、English、12345。',el('br'),el('code',{},'代码与表格跟随消息字号。'));
    const feedback=el('div');fontForm.append(preview,feedback,actions(button('恢复默认',()=>{for(const [key] of fontKeys)fontForm.elements[key].value=fonts.defaults[key];fontForm.querySelectorAll('[data-font-key]').forEach(slider=>slider.value=fonts.defaults[slider.dataset.fontKey]);state.dirty=true;fonts.preview(fonts.defaults);}),button('取消预览',()=>{fonts.cancelPreview();for(const [key] of fontKeys)fontForm.elements[key].value=fonts.state.values[key];fontForm.querySelectorAll('[data-font-key]').forEach(slider=>slider.value=fonts.state.values[slider.dataset.fontKey]);state.dirty=false;}),el('button',{class:'wb-btn primary',type:'submit'},'保存字号')));
    dirtyForm(fontForm);fontForm.addEventListener('input',()=>{const values={};for(const [key] of fontKeys){const n=Number(fontForm.elements[key].value),[min,max]=fonts.limits[key];if(Number.isInteger(n)&&n>=min&&n<=max)values[key]=n;}fonts.preview(values);});
    fontForm.onsubmit=e=>{e.preventDefault();submit(fontForm,async d=>{await fonts.save(Object.fromEntries(fontKeys.map(([key])=>[key,Number(d[key])])));feedback.replaceChildren(message('字号已保存，其他网页设备同步生效。','success'));});};
    appearance.append(fontForm);
    const visual=card('主题与布局',theme,reduce,button('重置布局宽度',()=>{localStorage.removeItem('xgent-detail-width');document.documentElement.style.setProperty('--detail-width','440px');}));
    grid.append(appearance,visual,card('快捷操作',el('dl',{},...[['Ctrl / ⌘ K','搜索命令'],['Ctrl / ⌘ F','搜索全部历史'],['Enter','桌面发送消息'],['Shift / Alt + Enter','换行'],['Esc','关闭详情或弹窗']].map(([k,v])=>el('div',{class:'wb-detail-row'},el('dt',{},k),el('dd',{},v)))),el('p',{class:'wb-note'},'触屏 Enter 始终换行。正在生成时仍可编辑草稿。')));
  }
  if(active==='conversation'||active==='agent'){
    const conversation=[['stream_mode','流式输出','bool'],['hide_protocol_blocks','默认收起工具块（展开仍完整）','bool'],['thinking_level','思考深度','select'],['text_stitch_mode','文本拼接方式','select'],['global_depth','记忆深度','number'],['stream_timeout','流式超时（秒，0 为自动）','number']];
    const agent=[['agent_mode','启用 Agent 自动执行','bool'],['agent_max_iterations','最大迭代次数','number'],['agent_command_timeout','命令等待时间（秒）','number'],['smart_match_threshold','智能匹配阈值','number'],['idle_message_interval','空闲提示间隔（秒）','number']];
    const fields=active==='agent'?agent:conversation;const form=el('form');
    const notes={"stream_mode":"开启后逐段显示回复，关闭后完整返回；不改变回复内容。","hide_protocol_blocks":"只决定工具执行块的初始展开状态，原文始终保留。","thinking_level":"选择模型思考深度；不同模型支持范围不同。","text_stitch_mode":"控制连续回复的拼接方式。","global_depth":"参与后续请求的历史深度，不会删除聊天记录。","stream_timeout":"等待模型回复的超时；0 使用自动策略。","agent_mode":"允许 Agent 按现有权限执行工具。","agent_max_iterations":"本轮请求允许的最多 Agent 迭代次数。","agent_command_timeout":"命令等待时间，单位为秒。","smart_match_threshold":"命令智能匹配阈值，范围 0–100%。","idle_message_interval":"空闲状态提示的间隔，单位为秒。"};
    for(const [key,label,type] of fields){const control=type==='bool'?check(label,key,settings.values[key]):type==='select'?select(label,key,settings.options[key],settings.values[key]):field(label,key,settings.values[key],type);const row=el('div',{class:'wb-setting-row'},control,el('p',{class:'wb-setting-note'},notes[key]));if(type==='number'){const input=control.querySelector('input');input.min=key==='global_depth'?'1':'0';input.step='1';if(key==='smart_match_threshold')input.max='100';}form.append(row);}
    const feedback=el('div');form.append(el('p',{class:'wb-note'},'保存后生效。执行中的命令不会因为修改参数而重新启动。'),feedback,el('button',{type:'submit',class:'wb-btn primary'},'保存设置'));dirtyForm(form);
    form.onsubmit=e=>{e.preventDefault();submit(form,async d=>{for(const [key,,type] of fields)await wb('settings',{data:{key,value:type==='number'?Number(d[key]):d[key]}});feedback.replaceChildren(message('设置已保存','success'));await refreshBootstrap();});};
    grid.append(card(active==='agent'?'执行行为':'对话行为',form));
    if(active==='agent') grid.append(card('执行权限说明',el('div',{class:'wb-callout'},el('strong',{},'这不是沙箱'),el('p',{},'Agent 与网页终端拥有运行账号的实际权限。命令黑名单不是安全隔离。请只执行你信任的任务。')),button('管理命令黑名单',()=>blacklistEditor({dialog,submit,dirtyForm}))));
  }
  if(active==='web'){
    const values=boot.web||{};const form=el('form',{},check('启用 Web Chat','web_enabled',values.web_enabled!==false),check('启用网页终端','terminal_enabled',values.terminal_enabled),field('监听端口','web_port',values.web_port||8790,'number','只监听 127.0.0.1，修改端口会使当前连接断开。'),field('公开 HTTPS 地址','web_public_url',values.web_public_url||'','url','用于 Telegram 网页入口；留空使用本地地址。'),el('p',{class:'wb-note'},'关闭 Web Chat 会失去当前管理界面。请确保还能通过 CLI 或 Telegram 重新开启。'),el('button',{class:'wb-btn primary',type:'submit'},'应用 Web 配置'));
    dirtyForm(form);form.onsubmit=e=>{e.preventDefault();if(!confirm('确认修改 Web 服务配置？端口改变或关闭功能可能让当前页面断线、关闭终端。'))return;submit(form,async d=>{const response=await wb('settings/web',{data:{...d,web_port:Number(d.web_port),confirm:true}});form.append(message(response.reconnect?'已保存，服务正在调整。请使用新的地址重新连接。':'配置已保存并同步。','success'));if(!response.reconnect)await refreshBootstrap();});};
    grid.append(card('服务入口',form));
    grid.append(card('账号与访问',el('p',{},'改密将撤销所有网页登录并关闭网页终端会话。'),button('修改访问密码',()=>{
      const f=el('form',{},field('新密码','password','','password','至少 6 位，建议使用长且唯一的密码。'),field('再次输入','repeat','','password'),check('我知道这会让全部浏览器下线并关闭终端','confirm'),el('button',{type:'submit',class:'wb-btn primary'},'保存并重新登录'));
      f.querySelectorAll('input[type=password]').forEach(n=>n.autocomplete='new-password');dirtyForm(f);
      f.onsubmit=e=>{e.preventDefault();submit(f,async d=>{if(d.password!==d.repeat)throw new Error('两次密码不一致');if(d.password.length<6)throw new Error('密码至少 6 位');await wb('password',{data:{password:d.password,confirm:d.confirm}});f.reset();state.dirty=false;sessionStorage.clear();f.append(message('已修改，正在撤销会话并重新登录…','success'));setTimeout(()=>location.reload(),1000);});};dialog('修改 Web 密码',f);
    }),button('退出当前登录',logout)));
  }
  if(active==='health'){
    grid.append(card('通道状态',el('p',{},'网页在线不代表 Telegram 或模型通道正常。各通道独立显示真实状态。'),...Object.entries(health.components||{}).map(([name,v])=>el('div',{class:'wb-detail-row'},el('dt',{},name),el('dd',{},tag(v.state),v.error?message(v.error):null))),button('刷新状态',render)));
    grid.append(el('details',{class:'wb-card wb-diagnostics'},el('summary',{},'查看详细健康数据'),el('pre',{class:'wb-pre'},JSON.stringify(health,null,2))));
    grid.append(card('维护操作',el('p',{},'操作将通过现有命令处理器执行，可能中断连接。'),button('检查服务状态',()=>{location.hash='/chat';window.XGentChat?.command('/status');}),button('重启服务',()=>confirmAction('重启服务','当前连接和终端可能被关闭。确认重启？',async()=>{location.hash='/chat';window.XGentChat?.command('/restart');}))));
  }
  page.append(grid);return page;
}

async function blacklistEditor({dialog,submit,dirtyForm}){
  const box=el('div',{},message('正在读取黑名单…','loading'));dialog('命令黑名单',box);
  try{
    let state=await wb('settings/blacklist');
    const form=el('form',{class:'wb-blacklist-form'},field('禁止片段（每行一条）','patterns',state.patterns.join('\n'),'textarea','命令包含这些片段时会被现有黑名单机制拦截；这不是沙箱。'));
    const input=form.querySelector('textarea');input.rows=10;input.setAttribute('aria-label','禁止片段（每行一条）');
    const recommended=el('details',{},el('summary',{},'推荐规则（不会自动启用）'),el('pre',{class:'wb-pre'},state.recommended.join('\n')),button('追加推荐规则',()=>{input.value=Array.from(new Set([...input.value.split('\n').filter(Boolean),...state.recommended])).join('\n');input.dispatchEvent(new Event('input',{bubbles:true}));}));
    const feedback=el('div');form.append(recommended,feedback,el('button',{class:'wb-btn primary',type:'submit'},'保存黑名单'));dirtyForm(form);
    form.onsubmit=e=>{e.preventDefault();const patterns=input.value.split('\n').map(s=>s.trim()).filter(Boolean);if(!patterns.length&&state.patterns.length&&!confirm('确认清空所有命令黑名单规则？'))return;submit(form,async()=>{state=await wb('settings/blacklist',{data:{patterns,revision:state.revision,confirm_clear:!patterns.length}});input.value=state.patterns.join('\n');feedback.replaceChildren(message('黑名单已保存','success'));});};box.replaceChildren(form);
  }catch(error){box.replaceChildren(message(error.message));}
}
