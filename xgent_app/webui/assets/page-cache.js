/** Small session-local LRU of mounted/detached workbench pages, never API secrets. */
export const PAGE_TTL = {tasks:15000, files:30000, models:60000, usage:60000, settings:60000};

export class PageCache {
  constructor(maxEntries=12, now=()=>Date.now()) {
    this.maxEntries=maxEntries;
    this.now=now;
    this.entries=new Map();
    this.revisions=new Map();
  }
  key(route,filters={}) {
    return JSON.stringify([route,Object.entries(filters).sort(([a],[b])=>a.localeCompare(b))]);
  }
  revision(route) { return this.revisions.get(route)||0; }
  get(key) {
    const entry=this.entries.get(key);
    if(entry){this.entries.delete(key);this.entries.set(key,entry);}
    return entry;
  }
  fresh(entry) {
    return !!entry && entry.revision===this.revision(entry.route) &&
      this.now()-entry.updatedAt < (PAGE_TTL[entry.route]||30000);
  }
  set(key,route,content,revision=this.revision(route),scroll=0) {
    const entry={route,content,revision,scroll,updatedAt:this.now(),interaction:0};
    this.entries.delete(key);this.entries.set(key,entry);
    while(this.entries.size>this.maxEntries)this.entries.delete(this.entries.keys().next().value);
    return entry;
  }
  invalidate(routes) {
    for(const route of routes)this.revisions.set(route,this.revision(route)+1);
  }
  delete(key) { this.entries.delete(key); }
  clear() { this.entries.clear();this.revisions.clear(); }
}

/** Mutations invalidate only dependent pages. Read-only POSTs do not invalidate. */
export function affectedPages(path,key) {
  if(path==='/api/config')path='/api/workbench/settings';
  if(path.startsWith('/api/workbench/providers/')) {
    return /\/(fetch|export)$/.test(path)?[]:['models','settings','usage'];
  }
  if(path.startsWith('/api/workbench/tasks/'))return ['tasks','files','usage'];
  if(path.startsWith('/api/workbench/skills/')||path.startsWith('/api/workbench/memories/'))return ['models','settings'];
  if(path==='/api/workbench/memory/clear')return ['tasks','files','usage','settings'];
  if(path==='/api/workbench/settings') {
    if(['model_price_table','model_merge_map','stats_auto_merge','stats_metric'].includes(key))return ['settings','usage'];
    if(['chat_model','skill_state','disabled_skills','hidden_skills'].includes(key))return ['settings','models'];
    return ['settings'];
  }
  if(path==='/api/workbench/settings/web'||path==='/api/workbench/password')return ['settings'];
  return [];
}
