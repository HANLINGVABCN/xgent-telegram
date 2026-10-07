"""Capture the workbench on desktop/tablet/mobile with isolated seeded data."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='workspace/web-workbench-qa')
    args=parser.parse_args();output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    from playwright.sync_api import sync_playwright
    process=subprocess.Popen([sys.executable,'-u',str(ROOT/'tools/workbench_fixture.py')],cwd=ROOT,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',
        env={**os.environ,'PYTHONIOENCODING':'utf-8'})
    lines=queue.Queue()
    def reader():
        for line in process.stdout:lines.put(line)
    threading.Thread(target=reader,daemon=True).start()
    try:
        fixture=None;deadline=time.time()+40
        while time.time()<deadline:
            try:line=lines.get(timeout=1)
            except queue.Empty:continue
            try:info=json.loads(line)
            except ValueError:continue
            if isinstance(info,dict) and 'url' in info:fixture=info;break
        if fixture is None:raise RuntimeError('Preview fixture did not start')
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            try:
                context=browser.new_context()
                assert context.request.post(fixture['url']+'/api/login',data={'password':fixture['password']}).ok
                context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(fixture['url']) else route.abort())
                page=context.new_page();errors=[];latencies=[];page.on('pageerror',lambda e:errors.append(str(e)))
                for width,height in [(1440,900),(768,1024),(390,844)]:
                    page.set_viewport_size({'width':width,'height':height})
                    for theme in ['light','dark']:
                        page.goto(fixture['url']);page.locator('#wb-chat-controls select').first.wait_for()
                        page.evaluate('(theme)=>{localStorage.setItem("xgent-theme",theme);document.body.classList.toggle("dark",theme==="dark");}',theme)
                        for route in ['chat','tasks','files','models','usage','settings']:
                            page.evaluate('(route)=>location.hash="/"+route',route)
                            if route!='chat':page.locator('#wb-page h1').wait_for()
                            if route=='files':
                                page.locator('#wb-page select[data-filter="kind"]').select_option('outputs')
                                page.get_by_role('button',name='查看日志',exact=True).first.wait_for()
                            page.wait_for_timeout(100)
                            if route=='chat':
                                page.locator('#log').evaluate('(e)=>e.scrollTop=0')
                                latencies.append(page.evaluate('''() => new Promise(resolve => {const started=performance.now();const input=document.getElementById("input");input.value="";input.dispatchEvent(new Event("input",{bubbles:true}));requestAnimationFrame(()=>resolve(performance.now()-started));})'''))
                            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                            page.screenshot(path=str(output/f'{width}-{theme}-{route}.png'))
                page.goto(fixture['url']+'/terminal');page.wait_for_timeout(500)
                page.screenshot(path=str(output/'terminal.png'))
                (output/'report.json').write_text(json.dumps({'page_errors':errors,'viewports':[1440,768,390],'themes':['light','dark'],'fixture':'isolated','input_feedback_ms_max':round(max(latencies),2)},ensure_ascii=False,indent=2),encoding='utf-8')
                if errors:raise AssertionError(errors)
            finally:browser.close()
        print(output)
    finally:process.terminate();process.wait(10)

if __name__=='__main__':main()
