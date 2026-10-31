import asyncio
from pathlib import Path
import sys
import time

from xgent_app.file_scan import run, scan


def test_600mb_log_has_bounded_memory_and_cancellation(tmp_path):
    path = tmp_path / 'large.log'
    block = (b'plain bounded line ' + b'a'*1004 + b'\n') * 64
    with path.open('wb') as handle:
        for _ in range((600*1024*1024+len(block)-1)//len(block)):
            handle.write(block)
    request = {'kind':'grep','path':str(path),'pattern':'NEVER_MATCH','regex':False,
               'ignore_case':False,'context_n':3,'max_hits':1,'glob':None}
    result = asyncio.run(run(request))
    assert not result['results']
    if sys.platform == 'linux':
        assert result['peak_rss_mb'] < 128
    async def cancel():
        stop = asyncio.Event()
        task = asyncio.create_task(run(request,stop))
        await asyncio.sleep(.15)
        started=time.monotonic(); stop.set()
        try:
            await task
        except ValueError as exc:
            assert '停止' in str(exc)
        assert time.monotonic()-started < 2
    asyncio.run(cancel())


def test_ranged_read_never_reads_binary_or_entire_large_file(tmp_path):
    path=tmp_path/'data.txt';path.write_text('one\ntwo\nthree\n',encoding='utf-8')
    assert scan({'kind':'read','path':str(path),'range':'2-2'})['lines']==['two']
    binary=tmp_path/'data.db';binary.write_bytes(b'\0SECRET')
    request={'kind':'grep','path':str(tmp_path),'pattern':'SECRET','regex':False,
             'ignore_case':False,'context_n':0,'max_hits':5,'glob':None}
    assert not scan(request)['results']


def test_worker_runs_outside_project_directory(tmp_path, monkeypatch):
    path=tmp_path/'read.txt';path.write_text('line one\nline two',encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    assert asyncio.run(run({'kind':'read','path':str(path),'range':'2-2'}))['lines']==['line two']
