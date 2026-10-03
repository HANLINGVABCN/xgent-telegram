"""Exercise the production transfer-state functions in Node, without a server."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "skill-public/script/webdav-filemanager/index.html").read_text(encoding="utf-8")


def helper(start, end):
    return HTML[HTML.rindex(start):HTML.index(end, HTML.rindex(start))]


@pytest.fixture
def run_ui():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for transfer UI regression tests")
    functions = "\n".join([
        helper("function normalizeTaskStatus(", "function focusRemoteDownload("),
        helper("function mergePersistedTask(", "function mergeRemoteTask("),
        helper("function processDownloadQueue(", "const UPLOAD_CHUNK_SIZE"),
        helper("function pickFileForResume(", "async function deleteDownloadTask("),
    ])
    setup = """
const assert = require('node:assert/strict');
let downloadTasks = [], selectedDownloadId = '', started = [];
let ignoredRemoteTaskIds = new Set();
function updateTransferBadge() {}
function startUploadTask(t) { started.push(t.id); }
function startDownloadTask(t) { started.push(t.id); }
function toast() {}
function fmtSize(n) { return String(n); }
"""
    def run(body):
        script = setup + functions + "\n(async () => {\n" + body + "\n})().catch(e => {console.error(e); process.exitCode=1;});"
        result = subprocess.run([node, "-"], input=script, text=True, capture_output=True,
                                encoding="utf-8", timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout
    return run


@pytest.mark.parametrize("status", ["downloading", "queued"])
def test_restored_upload_requires_resume_instead_of_blocking_queue(run_ui, status):
    run_ui(f"""
mergePersistedTask({{id:'up:1', kind:'upload', path:'/file', size:4, loaded:2, status:'{status}'}});
assert.equal(downloadTasks[0].status, 'paused');
assert.equal(downloadTasks[0].needsFile, true);
assert.equal(downloadTasks[0].serverTaskId, 'up:1');
downloadTasks.push({{id:'new', kind:'upload', status:'queued', file:{{}}}});
processDownloadQueue();
assert.deepEqual(started, ['new']);
""")


def test_live_upload_is_not_paused_by_polling(run_ui):
    run_ui("""
downloadTasks.push({id:'local', kind:'upload', path:'/file', status:'downloading',
                    controller:{}, file:{}, loaded:2});
mergePersistedTask({id:'up:1', kind:'upload', path:'/file', status:'paused', loaded:2, size:4});
assert.equal(downloadTasks[0].status, 'downloading');
assert.equal(downloadTasks[0].serverTaskId, 'up:1');
""")


def test_refreshed_existing_upload_does_not_revert_to_running(run_ui):
    run_ui("""
downloadTasks.push({id:'local', kind:'upload', path:'/file', status:'paused', controller:null});
mergePersistedTask({id:'up:1', kind:'upload', path:'/file', status:'downloading', loaded:2, size:4});
assert.equal(downloadTasks[0].status, 'paused');
assert.equal(downloadTasks[0].needsFile, true);
""")


@pytest.mark.parametrize("status", ["queued", "downloading"])
def test_trashed_task_neither_runs_nor_blocks_queue(run_ui, status):
    run_ui(f"""
downloadTasks.push({{id:'trash', kind:'upload', status:'{status}', trashed:true}});
downloadTasks.push({{id:'new', kind:'download', status:'queued'}});
processDownloadQueue();
assert.deepEqual(started, ['new']);
""")


def test_completed_snapshot_stays_complete(run_ui):
    run_ui("""
mergePersistedTask({id:'up:1', kind:'upload', path:'/file', status:'done', loaded:4, size:4});
assert.equal(downloadTasks[0].status, 'done');
assert.equal(downloadTasks[0].needsFile, false);
""")


def test_canceling_resume_file_picker_resolves(run_ui):
    run_ui("""
global.document = {createElement() {return {click() {this.oncancel?.();}};}};
const result = await Promise.race([pickFileForResume({size:4}),
    new Promise(resolve => setTimeout(() => resolve('unresolved'), 50))]);
assert.equal(result, null);
""")


def test_breadcrumb_keeps_quoted_directory_name_inside_attribute(run_ui):
    import json
    from html.parser import HTMLParser
    functions = helper('function renderBreadcrumb() {', '// Breadcrumb event delegation')
    functions += helper('function esc(s) {', 'function fmtSize')
    functions += helper('function escAttr(s) {', "document.addEventListener('DOMContentLoaded'")
    output = run_ui(functions + r'''
const crumb = {innerHTML: ''};
global.curPath = '/folder" data-extra="sentinel';
global.document = {
    getElementById() { return crumb; },
    createElement() { return {
        textContent: '',
        get innerHTML() { return String(this.textContent).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
    }; }
};
renderBreadcrumb();
console.log(JSON.stringify(crumb.innerHTML));
''')
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.buttons = []
        def handle_starttag(self, tag, attrs):
            if tag == 'button': self.buttons.append(dict(attrs))
    parser = Parser()
    parser.feed(json.loads(output))
    assert parser.buttons[-1]['data-nav-path'] == '/folder" data-extra="sentinel'
    assert 'data-extra' not in parser.buttons[-1]
