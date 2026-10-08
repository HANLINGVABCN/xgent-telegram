"""Slash suggestions execute once on Enter; Tab and IME remain non-executing."""
import pytest

from tests.test_workbench_browser import browser_context, workspace_url, ready  # noqa: F401


def command_page(context, url):
    page=context.new_page();sent=[]
    def receive(route):
        sent.append(route.request.post_data_json)
        route.fulfill(json={'ok':True})
    page.route('**/api/command',receive)
    ready(page,url)
    page.wait_for_function('!document.getElementById("btn-send").disabled')
    return page,sent


def test_slash_defaults_to_start_and_enter_sends_immediately(browser_context,workspace_url):
    page,sent=command_page(browser_context,workspace_url['url'])
    page.locator('#input').fill('/')
    first=page.locator('#cmd-suggest .cmd-item').first
    assert 'active' in first.get_attribute('class')
    assert first.locator('.c-cmd').text_content()=='/start'
    page.locator('#input').press('Enter')
    page.wait_for_timeout(100)
    assert sent==[{'conversation_id':'global_memory','command':'/start'}]
    assert page.locator('#input').input_value()==''


def test_prefix_arrow_selection_executes_exact_selected_command(browser_context,workspace_url):
    page,sent=command_page(browser_context,workspace_url['url'])
    page.locator('#input').fill('/s')
    page.locator('#input').press('ArrowDown')
    choice=page.locator('#cmd-suggest .cmd-item.active .c-cmd').text_content()
    page.locator('#input').press('Enter')
    page.wait_for_timeout(100)
    assert sent==[{'conversation_id':'global_memory','command':choice}]


def test_tab_completion_and_multiline_modifiers_do_not_send(browser_context,workspace_url):
    page,sent=command_page(browser_context,workspace_url['url'])
    page.locator('#input').fill('/s')
    selected=page.locator('#cmd-suggest .cmd-item.active .c-cmd').text_content()
    page.locator('#input').press('Tab')
    assert page.locator('#input').input_value()==selected+' '
    assert not sent
    page.locator('#input').fill('/')
    page.locator('#input').press('Shift+Enter')
    assert '\n' in page.locator('#input').input_value()
    assert not sent
    page.locator('#input').fill('/')
    page.locator('#input').press('Alt+Enter')
    assert '\n' in page.locator('#input').input_value()
    assert not sent
    page.locator('#input').fill('/')
    page.locator('#input').dispatch_event('keydown',{'key':'Enter','isComposing':True})
    assert not sent


def test_touch_slash_enter_sends_but_normal_text_enter_is_newline(browser_context,workspace_url):
    page=browser_context.new_page();sent=[]
    page.add_init_script("""const media=matchMedia.bind(window);window.matchMedia=q=>q==='(hover: none) and (pointer: coarse)'?{matches:true}:media(q);""")
    page.route('**/api/command',lambda r:(sent.append(r.request.post_data_json),r.fulfill(json={'ok':True})))
    ready(page,workspace_url['url']);page.wait_for_function('!document.getElementById("btn-send").disabled')
    page.locator('#input').fill('普通文字')
    page.locator('#input').press('Enter')
    assert page.locator('#input').input_value()=='普通文字\n'
    assert not sent
    page.locator('#input').fill('/')
    page.locator('#input').press('Enter')
    page.wait_for_timeout(100)
    assert sent==[{'conversation_id':'global_memory','command':'/start'}]


@pytest.mark.parametrize('width,height',[(390,844),(768,1024),(1440,900)])
def test_composer_menu_opens_shared_palette_and_preserves_draft(browser_context,workspace_url,width,height):
    page,sent=command_page(browser_context,workspace_url['url'])
    page.set_viewport_size({'width':width,'height':height})
    page.emulate_media(reduced_motion='reduce')
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    draft='未发送草稿：保留 **原文**'
    page.locator('#input').fill(draft)
    menu=page.locator('#btn-composer-menu')
    for theme in ['light','dark']:
        page.evaluate('(theme)=>document.body.classList.toggle("dark",theme==="dark")',theme)
        box=menu.bounding_box();attachment=page.locator('#btn-attach').bounding_box()
        text=page.locator('#input').bounding_box();send=page.locator('#btn-send').bounding_box()
        assert box['width']>=44 and box['height']>=44
        assert box['x']+box['width']<=attachment['x']
        assert attachment['x']+attachment['width']<=text['x']
        assert text['width']>=120 and text['x']+text['width']<=send['x']
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        menu.click()
        page.locator('#composer-commands .composer-command').first.wait_for()
        assert page.locator('#composer-commands .composer-command strong').first.inner_text()=='/start'
        assert page.locator('#input').input_value()==draft
        assert not sent
        assert not page.locator('#wb-dialog').is_visible()
        assert page.locator('#composer-commands .composer-command').first.evaluate('(n)=>n===document.activeElement')
        page.keyboard.press('Escape')
        assert not page.locator('#wb-dialog').is_visible()
        assert menu.evaluate('(n)=>n===document.activeElement')
        assert page.locator('#input').input_value()==draft
    assert not errors


def test_composer_menu_keyboard_selection_executes_once(browser_context,workspace_url):
    page,sent=command_page(browser_context,workspace_url['url'])
    menu=page.locator('#btn-composer-menu');menu.focus();menu.press('Enter')
    first=page.locator('#composer-commands .composer-command').first
    first.focus();first.press('Enter')
    page.wait_for_function('document.getElementById("composer-commands").hidden')
    assert sent==[{'conversation_id':'global_memory','command':'/start'}]
    assert page.locator('#input').input_value()==''
