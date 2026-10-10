"""Capture the requested composer card and its menus with isolated fixture data."""
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
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',default='workspace/composer-compact-qa');args=parser.parse_args()
    output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    process=subprocess.Popen([sys.executable,'-u',str(ROOT/'tools/workbench_fixture.py'),'--conversations','6','--workbench-demo'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHON_DOTENV_DISABLED':'1'})
    messages=queue.Queue();threading.Thread(target=lambda:[messages.put(line) for line in process.stdout],daemon=True).start()
    try:
        fixture=None;deadline=time.time()+40
        while time.time()<deadline:
            try:line=messages.get(timeout=1)
            except queue.Empty:continue
            try:data=json.loads(line)
            except ValueError:continue
            if 'url' in data:fixture=data;break
        if not fixture:raise RuntimeError('Fixture did not start')
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            context=browser.new_context(timezone_id='Asia/Shanghai')
            context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(fixture['url']) else r.abort())
            assert context.request.post(fixture['url']+'/api/login',data={'password':fixture['password']}).ok
            page=context.new_page();errors=[];sizes=[];page.on('pageerror',lambda e:errors.append(str(e)))
            for width,height in [(1440,900),(390,844),(360,640),(844,390)]:
                page.set_viewport_size({'width':width,'height':height});page.goto(fixture['url'])
                page.wait_for_function('window.XGentChat && !document.getElementById("composer-model").disabled')
                for dark in [False,True]:
                    theme='dark' if dark else 'light';page.evaluate('(dark)=>document.body.classList.toggle("dark",dark)',dark)
                    page.locator('#input').fill('');page.locator('#log').evaluate('(e)=>e.scrollTop=e.scrollHeight')
                    page.screenshot(path=str(output/f'{width}-{theme}-chat.png'))
                    page.locator('#composer').screenshot(path=str(output/f'{width}-{theme}-composer.png'))
                    sizes.append({'width':width,'theme':theme,'composer_height':page.locator('.composer-card').bounding_box()['height'],'footer_height':page.locator('#wb-shell > footer').bounding_box()['height']})
                    page.locator('#composer-model').click();page.screenshot(path=str(output/f'{width}-{theme}-model-menu.png'));page.keyboard.press('Escape')
                    page.locator('#composer-generation-options' if width<768 else '#composer-thinking').click();page.screenshot(path=str(output/f'{width}-{theme}-thinking-menu.png'));page.keyboard.press('Escape')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            browser.close()
            (output/'report.json').write_text(json.dumps({'page_errors':errors,'sizes':sizes,'fixture':'isolated'},ensure_ascii=False,indent=2),encoding='utf-8')
            assert not errors,errors
        print(output)
    finally:process.terminate();process.wait(10)

if __name__=='__main__':main()
