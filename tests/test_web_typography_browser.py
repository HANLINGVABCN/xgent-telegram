"""Semantic typography at desktop/mobile, both themes and every font-size bound."""
from pathlib import Path

import pytest

from tests.test_full_output_web import full_output_server  # noqa: F401
from tests.test_web_presentation import browser_session
from xgent_app.web_display import build_web_display


CODE = 'if ready:\n    print("two  spaces")\n\tprint("tab preserved")\n'
PROTOCOL = '```run-x\n<<BEGIN_LAYOUT12345\n' + '\n'.join(f'output {i}' for i in range(18)) + '\n<<END_LAYOUT12345\n```'
RAW = ('# 部署检查结果\n\n正文更扎实，信息分层，**重点清晰**。保留原来的工具折叠和复制操作。\n\n'
       '## 需要关注\n\n- 应用服务正常\n  - 数据库连接健康\n  - 保留现有配置\n- 网络稍有延迟\n\n'
       '> 先验证，再发布。\n\n| 检查项 | 状态 |\n|---|---|\n| 应用 | 正常 |\n| 网络 | 需关注 |\n\n'
       '---\n\n### 验证脚本\n\n```python\n' + CODE + '```\n\n' + PROTOCOL)


@pytest.mark.parametrize('viewport', [(1440, 1000), (390, 844)])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_semantics_weights_spacing_safe_rendering_and_screenshots(full_output_server, viewport, theme, tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    server = full_output_server[0]
    display = build_web_display(RAW)
    async def history(limit):
        return [
            {'id': 101, 'role': 'user', 'msg_type': 'user_text', 'content': '检查部署状态，不改变功能设计。', 'timestamp': 1},
            {'id': 102, 'role': 'assistant', 'msg_type': 'ai_reply', 'content': 'Telegram reduced HTML',
             'parse_mode': 'HTML', 'web_display': display, 'timestamp': 2},
            {'id': 103, 'role': 'assistant', 'msg_type': 'agent_result', 'content': '<b>验证完成</b>\n18 checks passed',
             'parse_mode': 'HTML', 'timestamp': 3},
            {'id': 104, 'role': 'system', 'msg_type': 'token_usage', 'content': '↑ 128 tokens · ↓ 64 tokens · 20 tokens/s', 'timestamp': 4},
        ]
    server.config.read_history = history
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Chromium unavailable')
        browser, context, base = browser_session(server, p)
        try:
            page = context.new_page()
            page.set_viewport_size({'width': viewport[0], 'height': viewport[1]})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.emulate_media(reduced_motion='reduce')
            page.goto(base)
            page.locator('[data-record-id="102"] h1').wait_for()
            page.evaluate('(theme)=>document.body.classList.toggle("dark",theme==="dark")', theme)
            body = page.locator('[data-record-id="102"] .body')
            assert body.locator('h1,h2,h3').count() == 3
            assert body.locator('ul ul li').count() == 2
            assert body.locator('table th').count() == 2
            assert body.locator('.code-block-wrap pre').text_content() == CODE
            assert body.locator('blockquote.pb').count() == 1
            for message_size, ui_size, terminal_size in [(16, 14, 13), (12, 12, 10), (24, 20, 24)]:
                page.evaluate('(v)=>window.XGentAppearance.preview(v)', {
                    'web_message_font_size': message_size, 'web_ui_font_size': ui_size, 'web_terminal_font_size': terminal_size})
                metrics = page.locator('[data-record-id="102"] .bubble').evaluate('''e=>{
                  const s=getComputedStyle(e),h=getComputedStyle(e.querySelector('h1'));
                  return [parseFloat(s.fontSize),s.fontWeight,parseFloat(s.lineHeight),parseFloat(h.fontSize),h.fontWeight,s.letterSpacing];
                }''')
                assert metrics[0] == message_size and metrics[1] == '500'
                assert abs(metrics[2] - message_size * 1.6) < .1
                assert metrics[3] > message_size and metrics[4] == '700' and metrics[5] == 'normal'
                assert page.locator('[data-record-id="102"]').evaluate('(e)=>getComputedStyle(e).marginBottom') == '16px'
                assert page.locator('[data-record-id="103"]').evaluate('(e)=>getComputedStyle(e).marginBottom') == '8px'
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                assert page.locator('#log').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
                page.locator('#log').evaluate('(e)=>e.scrollTop=0')
                page.screenshot(path=str(tmp_path / f'layout-{viewport[0]}-{theme}-{message_size}.png'))
            assert not errors
        finally:
            browser.close()


def test_stream_fragments_merge_semantically_and_preserve_expanded_blocks(full_output_server):
    playwright = pytest.importorskip('playwright.sync_api')
    server = full_output_server[0]
    async def history(limit): return []
    server.config.read_history = history
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Chromium unavailable')
        browser, context, base = browser_session(server, p)
        try:
            page = context.new_page(); page.goto(base)
            page.wait_for_function('!document.getElementById("btn-send").disabled')
            server.outbox.put({'type': 'message', 'message_id': 501, 'text': '<b>Telegram heading</b>', 'parse_mode': 'HTML',
                              'web_display': build_web_display(RAW, reply_message_id=501)})
            page.locator('blockquote.pb').wait_for()
            page.locator('blockquote.pb .pb-title').click()
            page.wait_for_function('document.querySelector("blockquote.pb").classList.contains("expanded")')
            page.locator('blockquote.pb').evaluate('(node)=>window.keptBlock=node')
            server.outbox.put({'type': 'message', 'message_id': 502, 'text': 'second Telegram fragment', 'parse_mode': 'HTML',
                              'web_display': build_web_display(RAW + '\n\n最后的补充。', reply_message_id=501)})
            page.get_by_text('最后的补充。', exact=True).wait_for()
            assert page.locator('#log>.msg-row').count() == 1
            assert page.evaluate('window.keptBlock.isConnected&&window.keptBlock.classList.contains("expanded")')
            assert page.locator('#log h1').count() == 1
            unsafe = {'version': 1, 'reply_message_id': 501, 'segments': [
                {'kind': 'markdown', 'text': '# 安全标题\n<script>window.hacked=1</script>\n[x](javascript:alert(1))'},
                {'kind': 'html', 'text': '<img src=x onerror="window.hacked=1"><b onclick="window.hacked=1">安全工具</b>'}]}
            server.outbox.put({'type': 'edit', 'message_id': 502, 'text': 'ignored', 'web_display': unsafe})
            page.get_by_text('安全工具', exact=True).wait_for()
            assert not page.evaluate('!!window.hacked')
            assert page.locator('.body [onclick],.body [onerror],.body a[href^="javascript:"]').count() == 0
        finally:
            browser.close()
