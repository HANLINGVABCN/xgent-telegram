import tempfile

from tests.test_thinking_params import run_in_app


def probe(name):
    with tempfile.TemporaryDirectory() as folder:
        result = run_in_app('import asyncio\nfrom tests import queue_request_probe as probe\n'
                            f'print(json.dumps(asyncio.run(probe.{name}(bot, {folder!r}))))')
    assert all(result.values()), result


def test_full_tool_results_then_ordered_queue_across_providers_and_over():
    probe('tools_and_over')


def test_native_telegram_timeout_cannot_lose_results_or_pause_queued_messages():
    probe('native_telegram_delivery_failure')


def test_native_storage_failure_still_pauses_and_can_resume_without_command_replay():
    probe('native_storage_failure_still_pauses')


def test_natural_end_batch_is_complete_in_all_stream_modes_and_not_reclassified_as_config():
    probe('natural_end')


def test_queue_failures_and_secret_retry_isolation():
    probe('failures_and_secrets')


def test_ask_submission_consumes_queue_but_cancellation_keeps_it_paused():
    probe('ask_queue')


def test_native_text_is_stitched_before_queue_and_done_callback_keeps_original_scope():
    probe('stitched_native_text')
