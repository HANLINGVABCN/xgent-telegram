"""Single-card composer layout and real configuration actions, using isolated data."""
import pytest
from tests.test_workbench_browser import workspace_url, browser_context, ready  # noqa: F401


@pytest.mark.parametrize('size', [(360,640),(390,844),(768,1024),(1440,900),(844,390)])
def test_single_card_never_has_external_control_rows(browser_context,workspace_url,size,tmp_path):
    page=browser_context.new_page();page.set_viewport_size({'width':size[0],'height':size[1]})
    ready(page,workspace_url['url']);page.wait_for_function('!document.getElementById("composer-model").disabled')
    page.evaluate('window.XGentAppearance.save(window.XGentAppearance.defaults)')
    page.locator('#input').fill('')
    assert page.locator('#composer select').count()==0
    assert page.locator('#composer .composer-card #wb-chat-controls').count()==1
    for dark in [False,True]:
        page.evaluate('(dark)=>document.body.classList.toggle("dark",dark)',dark)
        area=page.locator('#composer .composer-card').bounding_box()
        text=page.locator('#input').bounding_box();row=page.locator('.composer-row').bounding_box()
        assert text['y']+text['height']<=row['y']+1
        assert area['height']<=114
        for control in ['#btn-composer-menu','#btn-attach','#composer-model','#btn-send']:
            bounds=page.locator(control).bounding_box()
            assert bounds['x']>=area['x'] and bounds['x']+bounds['width']<=area['x']+area['width']+1
            assert bounds['y']>=row['y']-1 and bounds['y']+bounds['height']<=row['y']+row['height']+1
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        assert page.locator('#wb-shell > footer').bounding_box()['height']<=140
        if size[0]<768:
            assert page.locator('#composer-agent').is_hidden() and page.locator('#composer-thinking').is_hidden()
            assert page.locator('#composer-generation-options').is_visible()
        else:
            assert page.locator('#composer-agent').is_visible() and page.locator('#composer-thinking').is_visible()
        page.screenshot(path=str(tmp_path/f'composer-{size[0]}-{dark}.png'))
    for message,ui in [(12,12),(24,20)]:
        page.evaluate('([message,ui])=>window.XGentAppearance.save({web_message_font_size:message,web_ui_font_size:ui})',[message,ui])
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        assert page.locator('#input').is_visible()
        assert page.locator('#wb-shell > footer').bounding_box()['height']<=160
    page.evaluate('window.XGentAppearance.save(window.XGentAppearance.defaults)');page.close()


def test_popovers_save_real_values_preserve_draft_and_close(browser_context,workspace_url):
    page=browser_context.new_page();ready(page,workspace_url['url'])
    page.wait_for_function('!document.getElementById("composer-model").disabled')
    page.locator('#input').fill('消息草稿\n第二行')
    page.evaluate('window.composerInput=document.getElementById("input")')
    model=page.get_by_role('combobox',name='对话模型',exact=True);model.click()
    panel=page.locator('#composer-options-popover');panel.wait_for()
    bounds=panel.bounding_box();card=page.locator('#composer').bounding_box()
    assert bounds['y']>=0 and bounds['y']+bounds['height']<=card['y']+1
    page.locator('.composer-option[data-value="OpenAI|gpt-4.1-mini"]').click()
    page.wait_for_function('document.getElementById("composer-model").dataset.value==="OpenAI|gpt-4.1-mini"')
    assert panel.is_hidden()
    thinking=page.get_by_role('combobox',name='思考深度',exact=True)
    thinking.focus();thinking.press('ArrowDown')
    page.locator('.composer-option[data-value=high]').click()
    page.wait_for_function('document.getElementById("composer-thinking").dataset.value==="high"')
    assert panel.is_hidden()
    assert page.locator('#input').input_value()=='消息草稿\n第二行'
    assert page.evaluate('window.composerInput===document.getElementById("input")')
    model.click();page.keyboard.press('Escape')
    assert panel.is_hidden() and model.evaluate('(e)=>document.activeElement===e')
    model.click();page.locator('#input').click();assert panel.is_hidden()
    assert browser_context.request.get(workspace_url['url']+'/api/workbench/bootstrap').json()['settings']['values']['chat_model']=='OpenAI|gpt-4.1-mini'
    page.close()


def test_mobile_generation_panel_switch_and_resize(browser_context,workspace_url):
    page=browser_context.new_page();page.set_viewport_size({'width':390,'height':844});ready(page,workspace_url['url'])
    page.wait_for_function('!document.getElementById("composer-model").disabled')
    page.locator('#input').fill('手机草稿')
    original=browser_context.request.get(workspace_url['url']+'/api/workbench/bootstrap').json()['settings']['values']['agent_mode']
    trigger=page.get_by_role('button',name='生成选项',exact=True);trigger.click()
    switch=page.get_by_role('switch',name='Agent 自动执行',exact=True);switch.click()
    page.wait_for_function('(old)=>document.getElementById("composer-agent").getAttribute("aria-pressed")===String(!old)',arg=original)
    assert page.locator('#composer-options-popover').is_hidden()
    trigger.click();page.locator('.composer-option[data-value=auto]').click()
    page.wait_for_function('document.getElementById("composer-thinking").dataset.value==="auto"')
    trigger.click();page.set_viewport_size({'width':1024,'height':768})
    assert page.locator('#composer-options-popover').is_hidden()
    assert page.locator('#composer-thinking').is_visible()
    assert page.locator('#input').input_value()=='手机草稿'
    page.close()


def test_failed_change_stays_in_composer_and_preserves_value(browser_context,workspace_url):
    page=browser_context.new_page();ready(page,workspace_url['url'])
    page.wait_for_function('!document.getElementById("composer-model").disabled')
    value=page.locator('#composer-thinking').get_attribute('data-value');page.locator('#input').fill('不能丢失')
    page.route('**/api/workbench/settings',lambda r:r.fulfill(status=503,json={'error':'保存失败测试'}))
    page.locator('#composer-thinking').click();page.locator('.composer-option[data-value=low]').click()
    page.get_by_text('保存失败测试',exact=True).wait_for()
    assert page.locator('#composer-thinking').get_attribute('data-value')==value
    assert page.locator('#input').input_value()=='不能丢失'
    assert page.locator('#composer-option-error').is_visible()
    assert page.locator('#composer-options-popover').is_visible()
    page.keyboard.press('Escape');page.close()
