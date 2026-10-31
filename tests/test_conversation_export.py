from tests.test_thinking_params import run_in_app


def test_export_reuses_scoped_archive_without_changing_chat_or_selectors(tmp_path):
    result = run_in_app('import asyncio\nfrom tests.conversation_export_probe import exercise\n' +
                        f'print(json.dumps(asyncio.run(exercise(bot,{str(tmp_path)!r}))))')
    assert all(result.values())
