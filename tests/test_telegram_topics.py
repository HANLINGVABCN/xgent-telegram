import tempfile

from tests.test_thinking_params import run_in_app


def test_private_topics_keep_existing_history_routes_and_recovery_identity():
    with tempfile.TemporaryDirectory() as root:
        result=run_in_app('import asyncio\nfrom tests.telegram_topics_probe import exercise\n'
                          +f'print(json.dumps(asyncio.run(exercise(bot,{root!r}))))')
    assert result['schema']==9 and all(result.values()),result


def test_v8_database_is_backed_up_before_topic_mapping_migration():
    with tempfile.TemporaryDirectory() as root:
        result=run_in_app('import asyncio\nfrom tests.telegram_topics_probe import migration\n'
                          +f'print(json.dumps(asyncio.run(migration(bot,{root!r}))))')
    assert result['version']==9 and result['backup'] and result['table'],result


def test_native_a_and_b_pages_execute_independently_and_reply_to_own_threads():
    with tempfile.TemporaryDirectory() as root:
        result=run_in_app('import asyncio\nfrom tests.telegram_topics_probe import parallel_pages\n'
                          +f'print(json.dumps(asyncio.run(parallel_pages(bot,{root!r}))))')
    assert all(result.values()),result
