import {el,button,fmt,field,table,wb,query,message,empty} from './components.js';

const money=value=>value==null?'未配置':'$'+Number(value).toFixed(6);
const percent=value=>(value*100).toFixed(1)+'%';
function localDate(ts,zone){
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(ts*1000));
  const get=type=>parts.find(p=>p.type===type).value;return `${get('year')}-${get('month')}-${get('day')}`;
}
function time(ts,zone){return new Date(ts*1000).toLocaleString('zh-CN',{timeZone:zone,hour12:false});}
function scrollTable(headings,rows,label){const node=table(headings,rows);node.tabIndex=0;node.setAttribute('role','region');node.setAttribute('aria-label',label);return node;}

export async function usageView(read,{filter,render,heading,card,actions,priceSettings,state}){
  const range=filter('range')||'7',model=filter('model')||'';
  const data=await read('usage'+query({range,start_date:filter('start_date'),end_date:filter('end_date'),model}));
  const zone=data.timezone||'Asia/Shanghai',a=data.summary;
  const page=el('div',{class:'wb-usage-page'},heading('用量与成本','对照 Token 报表查看调用、缓存、思考与费用；清空聊天不会清空用量。',[button('配置价格与合并规则',priceSettings)]));
  const controls=el('section',{class:'wb-usage-controls','aria-label':'统计筛选'}),presets=el('div',{class:'wb-range-options',role:'group','aria-label':'时间范围'});
  const dateForm=el('form',{class:'wb-usage-dates'},field('开始日期','start_date',localDate(data.start,zone),'date'),field('结束日期','end_date',localDate(data.end,zone),'date'));
  const apply=el('button',{class:'wb-btn primary',type:'submit'},'应用日期');
  const feedback=el('div',{'aria-live':'polite',class:'wb-range-feedback'});
  function dateState(value){
    for(const btn of presets.children)btn.setAttribute('aria-pressed',String(btn.dataset.range===value));
    for(const input of dateForm.querySelectorAll('input')){input.readOnly=value!=='custom';input.required=true;}
    apply.hidden=value!=='custom';
  }
  function refreshRange(value){
    if(value==='custom'){dateState(value);dateForm.elements.start_date.focus();return;}
    state.dirty=false;filter('range',value);filter('start_date','');filter('end_date','');render();
  }
  for(const [value,label] of [['7','最近 7 天'],['30','最近 30 天'],['90','最近 90 天'],['all','全部'],['custom','自定义范围']]){
    const btn=button(label,()=>refreshRange(value));btn.dataset.range=value;presets.append(btn);
  }
  const select=el('select',{'aria-label':'模型筛选','data-filter':'model'},el('option',{value:''},'全部模型'),data.models.map(m=>el('option',{value:m},m)));
  if(model&&!data.models.includes(model))select.append(el('option',{value:model},model+'（此范围无记录）'));
  select.value=model;select.onchange=()=>{if(state.dirty&&!confirm('日期尚未应用，放弃修改？')){select.value=model;return;}state.dirty=false;filter('model',select.value);render();};
  dateForm.append(apply);dateState(range);
  if(range==='custom'){dateForm.elements.start_date.value=filter('start_date')||localDate(data.start,zone);dateForm.elements.end_date.value=filter('end_date')||localDate(data.end,zone);}
  dateForm.addEventListener('input',()=>{state.dirty=true;feedback.replaceChildren(message('日期尚未应用，当前图表仍为上次查询结果。','loading'));});
  dateForm.onsubmit=e=>{e.preventDefault();const start=dateForm.elements.start_date.value,end=dateForm.elements.end_date.value;
    if(!start||!end||start>end){feedback.replaceChildren(message('请选择有效的起止日期，开始日期不能晚于结束日期。'));return;}
    state.dirty=false;filter('range','custom');filter('start_date',start);filter('end_date',end);render();
  };
  controls.append(el('div',{class:'wb-usage-filter-row'},presets,el('label',{class:'wb-field'},el('span',{},'模型筛选'),select)),dateForm,
    el('p',{class:'wb-note wb-range-exact'},`实际统计：${time(data.start,zone)} — ${time(data.end,zone)} · ${zone}`),feedback);
  if(range==='all')controls.append(el('p',{class:'wb-note'},a.count?'全部已存盘用量，从最早一条记录至当前时间。':'暂无用量记录，日期暂显示当前时间。'));
  page.append(controls);
  const stats=[['调用次数',fmt(a.count),'当前筛选内全部调用'],['总 Token',fmt(a.total),'沿用模型返回的总计'],['输入 Token',fmt(a.input),'按原报表计费口径'],['输出 Token',fmt(a.output),'包含上游报告的思考用量'],['缓存 Token',fmt(a.cached),'上游报告的缓存命中'],['其中思考',fmt(a.reasoning),'输出的子项，不重复相加'],['预估总费用',money(a.cost),'USD · 配置价格估算'],['单次均费',money(a.average_cost),'费用 ÷ 调用次数'],['缓存省下',money(a.cache_saved),'缓存量 × 输入与缓存价差']];
  page.append(el('div',{class:'wb-usage-stats'},stats.map(([label,value,note])=>el('div',{class:'wb-stat'},el('label',{},label),el('strong',{},value),el('small',{},note)))));
  if(data.missing_prices.length)page.append(message('以下模型价格未配置：'+data.missing_prices.join('、')+'。总费用、均费和缓存节省不冒充零值；已配置部分费用为 '+money(a.known_cost)+'。','error'));
  const charts=el('div',{class:'wb-usage-charts'});
  const trend=card('用量趋势'),distribution=card('模型分布');charts.append(trend,distribution);page.append(charts);
  function metricControl(draw){const row=el('div',{class:'wb-metric-switch',role:'group','aria-label':'图表指标'});for(const [metric,label] of [['total','Token'],['cost','费用']]){const btn=button(label,()=>{for(const b of row.children)b.setAttribute('aria-pressed',String(b===btn));draw(metric);});btn.setAttribute('aria-pressed',String(metric==='total'));row.append(btn);}return row;}
  const trendBody=el('div');trend.append(metricControl(drawTrend),trendBody);
  function drawTrend(metric){
    trendBody.replaceChildren();if(!data.days.length){trendBody.append(empty('还没有用量记录','调整筛选或开始对话后再查看。'));return;}
    const max=Math.max(1,...data.days.map(d=>d[metric]||0));
    const chart=el('div',{class:'wb-usage-bars',role:'img','aria-label':`${data.bucket==='month'?'每月':'每日'}${metric==='cost'?'费用':'Token'}趋势，具体数值见下方时间汇总`});
    for(const d of data.days){const col=el('div',{class:'wb-usage-bar-col',title:`${d.day} · ${metric==='cost'?money(d.cost):fmt(d.total)} · ${fmt(d.count)} 次调用`}),bar=el('div',{class:'wb-usage-bar'+(d[metric]==null?' unpriced':'')});bar.style.height=(d[metric]==null?2:Math.max(1,d[metric]/max*100))+'%';col.append(bar,el('small',{},data.days.length<=15?d.day.slice(5)||d.day:''));chart.append(col);}
    trendBody.append(chart,el('p',{class:'wb-note'},`${data.bucket==='month'?'按月汇总（跨度超过一年）':'按日汇总'} · ${data.days[0].day} — ${data.days.at(-1).day} · 仅绘制有调用的日期`));
  }
  const distributionBody=el('div',{class:'wb-distribution'});distribution.append(metricControl(drawDistribution),distributionBody);
  function drawDistribution(metric){
    const rows=data.per_model.slice().sort((a,b)=>(b[metric]??-1)-(a[metric]??-1));const total=rows.reduce((sum,m)=>sum+(m[metric]||0),0);
    distributionBody.replaceChildren();if(!rows.length){distributionBody.append(empty('暂无模型用量','当前筛选没有调用。'));return;}
    for(const m of rows){const bar=el('div',{class:'wb-share-bar'});bar.style.width=(total?(m[metric]||0)/total*100:0)+'%';distributionBody.append(el('div',{class:'wb-share-item'},el('div',{},el('strong',{},m.model),el('span',{},metric==='cost'?money(m.cost):fmt(m.total)+' · '+percent(m.share))),el('div',{class:'wb-share-track'},bar)));}
    if(metric==='cost'&&data.missing_prices.length)distributionBody.append(el('p',{class:'wb-note'},'费用占比只计算已配置价格的模型。'));
  }
  drawTrend('total');drawDistribution('total');
  const modelSection=card('模型用量明细'),modelBody=el('div');
  const sort=el('select',{'aria-label':'模型排序'},el('option',{value:'total'},'按总 Token'),el('option',{value:'cost'},'按费用'),el('option',{value:'count'},'按调用次数'),el('option',{value:'model'},'按模型名称'));
  modelSection.append(el('div',{class:'wb-usage-table-head'},el('p',{class:'wb-note'},`自动合并${data.auto_merge?'开启':'关闭'}，手动合并规则优先。单价：USD / 百万 Token。`),sort),modelBody);page.append(modelSection);
  const headings=['模型 / 原始名称','调用','输入','输出','缓存','其中思考','总计','占比','费用','单次均费','输入 / 输出 / 缓存单价'];
  function drawModels(){const rows=data.per_model.slice().sort((a,b)=>sort.value==='model'?a.model.localeCompare(b.model):(b[sort.value]??-1)-(a[sort.value]??-1));
    modelBody.replaceChildren(scrollTable(headings,rows.map(m=>[el('div',{},el('strong',{},m.model),el('small',{},m.members.join('、'))),fmt(m.count),fmt(m.input),fmt(m.output),fmt(m.cached),fmt(m.reasoning),fmt(m.total),percent(m.share),money(m.cost),money(m.average_cost),m.price?`$${m.price.input} / $${m.price.output} / $${m.price.cached}`:'未配置']).concat([[el('strong',{},'合计'),fmt(a.count),fmt(a.input),fmt(a.output),fmt(a.cached),fmt(a.reasoning),fmt(a.total),a.total?'100%':'—',money(a.cost),money(a.average_cost),'—']]),'模型用量明细（可横向滚动）'));
  }
  sort.onchange=drawModels;drawModels();
  const daily=el('details',{class:'wb-card wb-usage-daily'},el('summary',{},`${data.bucket==='month'?'每月':'每日'}汇总 · ${data.days.length} 个有调用的时段`));
  daily.append(scrollTable(['日期','调用','输入','输出','缓存','思考','总计','费用'],data.days.map(d=>[d.day,fmt(d.count),fmt(d.input),fmt(d.output),fmt(d.cached),fmt(d.reasoning),fmt(d.total),money(d.cost)]),'时间汇总（可横向滚动）'));page.append(daily);
  const calls=card('最近调用明细');const row=r=>[time(r.ts,zone),el('div',{},r.model,r.display_model!==r.model?el('small',{},'合并为 '+r.display_model):null),fmt(r.input??r.input_tokens),fmt(r.output??r.output_tokens),fmt(r.cached??r.cached_tokens),fmt(r.reasoning??r.reasoning_tokens),fmt(r.total??r.total_tokens),money(r.cost)];
  const callTable=scrollTable(['时间','原始模型','输入','输出','缓存','思考','总计','费用'],data.records.map(row),'调用明细（可横向滚动）'),status=el('p',{class:'wb-note',role:'status'});
  calls.append(callTable,status);page.append(calls);let cursor=data.next_records_cursor,loaded=data.records.length;
  const more=button('加载更早的调用明细',async()=>{more.disabled=true;more.textContent='正在读取…';failure.replaceChildren();const controller=new AbortController();const abort=()=>{if(document.hidden||location.hash.replace(/^#\/?/,'')!=='usage')controller.abort();};document.addEventListener('visibilitychange',abort);window.addEventListener('hashchange',abort);
    try{const next=await wb('usage/records'+query({start:data.start,end:data.end,model,before:cursor}),{signal:controller.signal});if(!calls.isConnected)return;for(const r of next.items)callTable.querySelector('tbody').append(el('tr',{},row(r).map(value=>el('td',{},value))));loaded+=next.items.length;cursor=next.next_cursor;updateCalls();}
    catch(e){if(e.name!=='AbortError')failure.replaceChildren(message('明细加载失败：'+e.message));}
    finally{document.removeEventListener('visibilitychange',abort);window.removeEventListener('hashchange',abort);more.disabled=false;more.textContent='加载更早的调用明细';}
  });
  const failure=el('div');calls.append(failure,more);function updateCalls(){status.textContent=`已显示 ${fmt(loaded)} / ${fmt(a.count)} 次调用；每次加载 50 条，汇总不受明细分页影响。`;more.hidden=!cursor;callTable.hidden=loaded===0;}updateCalls();
  if(!a.count)calls.append(empty('没有调用记录','当前时间和模型筛选下没有结果。'));
  page.append(el('p',{class:'wb-note wb-usage-method'},'计费沿用导出报表：输入量 × 输入价 + 输出量 × 输出价 + 缓存量 × 缓存价（每百万 Token）；缓存节省 = 缓存量 × max(输入价 − 缓存价, 0)。缓存与思考为上游报告字段，不再次加到总 Token；这里是估算，不是供应商账单。'));
  return page;
}
