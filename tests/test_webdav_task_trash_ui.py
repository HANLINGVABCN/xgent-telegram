"""Test actual task mutation handlers, including their server acknowledgements."""
from tests.test_webdav_transfer_ui import helper, run_ui


BASE = helper('function taskRecordPayload(', 'function syncTrashActions(') + """
let taskMutationPending = 0, taskMutationVersion = 0;
let currentTransferTab = 'remote', currentTransferFilter = 'all';
let calls = [], notices = [], saved = 0;
function persistLocalTasks() { saved++; }
function finishDownloadActivity() {}
function renderDownloadQueue() {}
function renderDownloadDetails() {}
function selectedDownloadTask() { return null; }
function syncTrashActions() {}
function setTransferTab(tab) {currentTransferTab = tab;}
function setTransferFilter(tab) {currentTransferFilter = tab;}
function toast(message, kind) {notices.push({message, kind});}
async function apiFetch(url, options) {
    calls.push({url, body: JSON.parse(options.body)});
    return {success: true, task: {status:'paused'}};
}
"""


def test_delete_posts_canonical_id_before_marking_trash(run_ui):
    run_ui(BASE + """
const t = {id:'local', remoteId:'server', kind:'remote', status:'done', path:'/x'};
downloadTasks.push(t);
assert.equal(await trashTask(t), true);
assert.equal(calls[0].url, '/api/tasks-delete');
assert.equal(calls[0].body.items[0].id, 'server');
assert.equal(calls[0].body.trash, true);
assert.equal(t.trashed, true);
assert(saved > 0);
""")


def test_failed_delete_keeps_task_and_reports_error(run_ui):
    run_ui(BASE + """
const t = {id:'server', kind:'remote', status:'done'};
downloadTasks.push(t);
apiFetch = async () => {throw new Error('offline');};
assert.equal(await trashTask(t), false);
assert.equal(t.trashed, undefined);
assert.equal(saved, 0);
assert.equal(notices.at(-1).kind, 'error');
assert.equal(taskMutationPending, 0);
""")


def test_purge_failure_does_not_fake_empty_trash(run_ui):
    run_ui(BASE + """
const t = {id:'server', kind:'remote', status:'done', trashed:true};
downloadTasks.push(t);
apiFetch = async () => {throw new Error('offline');};
assert.equal(await purgeTask(t), false);
assert.equal(downloadTasks.length, 1);
assert.equal(t.trashed, true);
assert.equal(saved, 0);
""")


def test_clear_trash_persists_one_batch_then_removes_local_rows(run_ui):
    handlers = helper('async function clearTrash()', 'async function loadShares()')
    run_ui(BASE + handlers + """
let confirmAction;
function showConfirm(title, text, callback) {confirmAction = callback;}
downloadTasks.push({id:'one', kind:'remote', trashed:true}, {id:'two', kind:'remote', trashed:true});
await clearTrash();
assert.equal(calls.length, 0);
await confirmAction();
assert.equal(calls.length, 1);
assert.equal(calls[0].body.trash, false);
assert.equal(calls[0].body.items.length, 2);
assert.equal(downloadTasks.length, 0);
assert(ignoredRemoteTaskIds.has('one'));
assert(ignoredRemoteTaskIds.has('two'));
""")


def test_restore_is_sent_to_server_with_upload_server_id(run_ui):
    run_ui(BASE + """
const t = {id:'local', serverTaskId:'up:server', kind:'upload', status:'canceled', trashed:true};
downloadTasks.push(t);
assert.equal(await restoreTask(t), true);
assert.equal(calls[0].url, '/api/tasks-restore');
assert.equal(calls[0].body.id, 'up:server');
assert.equal(t.trashed, false);
assert.equal(t.status, 'paused');
""")


def test_stale_poll_response_cannot_reinsert_purged_task(run_ui):
    poll = helper('async function pollAllTasks()', 'async function loadAllTasks()')
    run_ui(BASE + poll + """
function stopRemotePollingIfIdle() {}
function startRemotePolling() {}
function mergeRemoteTask(t) { downloadTasks.push(t); }
let finishPoll;
apiFetch = async (url) => {
    if (url === '/api/tasks') return new Promise(resolve => {finishPoll = resolve;});
    return {success:true};
};
const t = {id:'server', kind:'remote', trashed:true};
downloadTasks.push(t);
const oldPoll = pollAllTasks();
await purgeTask(t);
finishPoll({items:[{id:'server', kind:'remote', status:'done'}]});
await oldPoll;
assert.equal(downloadTasks.length, 0);
""")


def test_background_file_list_does_not_leave_restored_transfer_page(run_ui):
    load = helper('async function loadFiles(path, opts = {})', 'function goBack()')
    run_ui(BASE + load + """
let currentView = 'download', curPath = '/', files = [], selected = new Set(), historyCalls = 0;
function showLoading() {}
function renderBreadcrumb() {}
function renderFiles() {}
function pushAppHistory() {historyCalls++;}
function showBrowserPage() {currentView = 'browser';}
apiFetch = async () => ({path:'/', items:[]});
await loadFiles('/', {background:true, replaceHistory:true});
assert.equal(currentView, 'download');
assert.equal(historyCalls, 0);
""")


def test_late_file_list_does_not_undo_user_navigation(run_ui):
    load = helper('async function loadFiles(path, opts = {})', 'function goBack()')
    run_ui(BASE + load + """
let currentView = 'browser', curPath = '/', files = [], selected = new Set(), finish;
function showLoading() {}
function renderBreadcrumb() {}
function renderFiles() {}
function pushAppHistory() {throw new Error('stale navigation');}
function showBrowserPage() {currentView = 'browser';}
apiFetch = async () => new Promise(resolve => finish = resolve);
const pending = loadFiles('/');
currentView = 'download';
finish({path:'/', items:[]});
await pending;
assert.equal(currentView, 'download');
""")


def test_task_and_share_trash_buttons_have_distinct_ids():
    from html.parser import HTMLParser
    from tests.test_webdav_transfer_ui import HTML
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = []
        def handle_starttag(self, tag, attrs):
            if tag == 'button': self.ids.append(dict(attrs).get('id'))
    parser = Parser()
    parser.feed(HTML)
    assert parser.ids.count('clearTrashBtn') == 1
    assert parser.ids.count('clearShareTrashBtn') == 1
