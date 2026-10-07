import {el,button,field,select,check,wb,message,tag} from './components.js';

export async function settingsView({logout,boot,health,state,render,dialog,closeDialog,submit,dirtyForm,confirmAction,refreshBootstrap,setTheme,filter,heading,card,actions}) {
  const settings=boot.settings;
  const page=el('div',{},heading('设置','业务设置与 Telegram、CLI 共享。外观偏好仅影响当前浏览器。',[button('退出登录',logout,'danger')]));
  const sections=[['appearance','外观'],['conversation','对话与记忆'],['agent','Agent'],['web','Web 与安全'],['health','系统状态']];
  const active=filter('section')||'appearance';
  page.append(el('div',{class:'wb-tabs',role:'tablist'},sections.map(([id,title])=>el('button',{role:'tab','aria-selected':id===active,class:id===active?'active':'',onclick:()=>{if(state.dirty&&!confirm('尚有未保存设置，放弃修改？'))return;state.dirty=false;filter('section',id);render();}},title))));
  const grid=el('div',{class:'wb-settings-grid'});
  if(active==='appearance'){
    const appearance=card('让工作台适合你的习惯',el('p',{},'桌面、手机与 Telegram 内嵌页使用同一套可访问的视觉系统。'));
    const theme=select('主题','theme',[{value:'system',label:'跟随系统'},{value:'light',label:'浅色'},{value:'dark',label:'深色'}],localStorage.getItem('xgent-theme')||'system');theme.querySelector('select').onchange=e=>setTheme(e.target.value);
    const reduce=check('减少界面动效','reduce',localStorage.getItem('xgent-reduce-motion')==='1');reduce.onchange=e=>{localStorage.setItem('xgent-reduce-motion',e.target.checked?'1':'0');document.body.classList.toggle('wb-reduce-motion',e.target.checked);};
    appearance.append(theme,reduce,button('重置布局宽度',()=>{localStorage.removeItem('xgent-detail-width');document.documentElement.style.setProperty('--detail-width','440px');}));
    grid.append(appearance,card('快捷操作',el('dl',{},...[['Ctrl / ⌘ K','搜索命令'],['Ctrl / ⌘ F','搜索全部历史'],['Enter','桌面发送消息'],['Shift / Alt + Enter','换行'],['Esc','关闭详情或弹窗']].map(([k,v])=>el('div',{class:'wb-detail-row'},el('dt',{},k),el('dd',{},v)))),el('p',{class:'wb-note'},'触屏 Enter 始终换行。正在生成时仍可编辑草稿。')));
  }
  if(active==='conversation'||active==='agent'){
    const conversation=[['stream_mode','流式输出','bool'],['hide_protocol_blocks','默认收起工具块（展开仍完整）','bool'],['thinking_level','思考深度','select'],['text_stitch_mode','文本拼接方式','select'],['global_depth','记忆深度','number'],['stream_timeout','流式超时（秒，0 为自动）','number']];
    const agent=[['agent_mode','启用 Agent 自动执行','bool'],['agent_max_iterations','最大迭代次数','number'],['agent_command_timeout','命令等待时间（秒）','number'],['smart_match_threshold','智能匹配阈值','number'],['idle_message_interval','空闲提示间隔（秒）','number']];
    const fields=active==='agent'?agent:conversation;const form=el('form');
    for(const [key,label,type] of fields)form.append(type==='bool'?check(label,key,settings.values[key]):type==='select'?select(label,key,settings.options[key],settings.values[key]):field(label,key,settings.values[key],type));
    const feedback=el('div');form.append(el('p',{class:'wb-note'},'保存后生效。执行中的命令不会因为修改参数而重新启动。'),feedback,el('button',{type:'submit',class:'wb-btn primary'},'保存设置'));dirtyForm(form);
    form.onsubmit=e=>{e.preventDefault();submit(form,async d=>{for(const [key,,type] of fields)await wb('settings',{data:{key,value:type==='number'?Number(d[key]):d[key]}});feedback.replaceChildren(message('设置已保存','success'));await refreshBootstrap();});};
    grid.append(card(active==='agent'?'执行行为':'对话行为',form));
    if(active==='conversation'){
      const danger=card('一份共享记忆',el('p',{},'当前不是独立多会话。清空将同时影响网页、Telegram 和 CLI 的共享上下文与显示记录；模型价格和用量统计不受影响。'),
        button('清空共享记忆',()=>confirmAction('清空共享记忆','清空后无法恢复，先确认不再需要当前上下文。',async()=>{await wb('memory/clear',{data:{confirm:'清空共享记忆'}});await window.XGentChat?.reload();},'清空共享记忆'),'danger'));danger.classList.add('wb-danger-zone');grid.append(danger);
    }else grid.append(card('执行权限说明',el('div',{class:'wb-callout'},el('strong',{},'这不是沙箱'),el('p',{},'Agent 与网页终端拥有运行账号的实际权限。命令黑名单不是安全隔离。请只执行你信任的任务。')),button('管理命令黑名单',()=>{location.hash='/chat';window.XGentChat?.command('/blacklist');})));
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
    grid.append(card('完整健康快照',el('pre',{class:'wb-pre'},JSON.stringify(health,null,2))));
    grid.append(card('维护操作',el('p',{},'操作将通过现有命令处理器执行，可能中断连接。'),button('检查服务状态',()=>{location.hash='/chat';window.XGentChat?.command('/status');}),button('重启服务',()=>confirmAction('重启服务','当前连接和终端可能被关闭。确认重启？',async()=>{location.hash='/chat';window.XGentChat?.command('/restart');}))));
  }
  page.append(grid);return page;
}
