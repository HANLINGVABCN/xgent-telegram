import tempfile

from xgent_app.miniapps import make_session, verify_session, form_display
from tests.test_thinking_params import run_in_app


def test_mini_session_is_signed_expires_and_is_bound_to_user_and_page():
    key=b'mini-session-test-key'
    token=make_session(key,1,'form',{'id':'abc','conversation_id':'c','generation':1},now=100)
    assert verify_session(key,token,1,kind='form',now=101)['resource']['id']=='abc'
    assert verify_session(key,token+'x',1,kind='form',now=101) is None
    assert verify_session(key,token,2,kind='form',now=101) is None
    assert verify_session(key,token,1,kind='tasks',now=101) is None
    assert verify_session(key,token,1,kind='form',now=3700) is None


def test_form_link_is_a_dedicated_https_page_and_offline_keeps_old_controls():
    from types import SimpleNamespace
    data={'web_enabled':True,'_web_has_password':True,'web_public_url':'https://example.com/xgent'}
    namespace={'UserDataManager':data}
    pending=SimpleNamespace(ask_id='abcdef',conversation_id='c',form=SimpleNamespace(questions=[1,2]))
    display=form_display(namespace,pending)
    assert display['reply_markup'][0][0]['url']=='https://example.com/xgent/mini/form?id=abcdef&conversation_id=c'
    assert display['reply_markup'][0][0]['web_app']
    assert form_display(namespace,pending,'submitted')['reply_markup'] is None
    data['web_public_url']='http://example.com'
    assert form_display(namespace,pending) is None


def test_mini_form_http_scope_refresh_submit_retry_and_secret_isolation():
    with tempfile.TemporaryDirectory() as root:
        result=run_in_app('import asyncio\nfrom tests.miniapp_probe import exercise\n'
                          +f'print(json.dumps(asyncio.run(exercise(bot,{root!r}))))')
    assert all(result.values()),result


def test_standalone_form_mobile_desktop_and_native_submit(tmp_path):
    import pytest
    pytest.importorskip('playwright.async_api')
    with tempfile.TemporaryDirectory() as root:
        result=run_in_app('import asyncio\nfrom tests.miniapp_browser_probe import exercise\n'
                          +f'print(json.dumps(asyncio.run(exercise(bot,{root!r},{str(tmp_path)!r}))))')
    if result.get('skip'):pytest.skip('Chromium unavailable')
    assert [case['width'] for case in result['cases']]==[390,1440]


def test_disconnect_during_submission_finishes_original_continuation_once():
    with tempfile.TemporaryDirectory() as root:
        result=run_in_app('import asyncio\nfrom tests.miniapp_probe import disconnected_submit\n'
                          +f'print(json.dumps(asyncio.run(disconnected_submit(bot,{root!r}))))')
    assert result['resumed_once'] and result['submitted'],result
