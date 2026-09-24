"""Run real menu handlers against isolated storage; never contact Telegram/providers."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def serve(root, port, forever):
    os.environ['BOT_TOKEN'] = '123456:fixture-only'
    os.environ['AUTHORIZED_USER_ID'] = '1'
    os.environ['XGENT_TRACE_LOG_FILE'] = str(Path(root) / 'trace.log')
    with contextlib.redirect_stdout(sys.stderr):
        import xgent_server as bot
    from tests.compression_request_probe import CompressionHarness
    from tests.ui_history_probe import prepare, menus
    from xgent_app.ui_history import UiHistoryError, ui_operation
    from xgent_app.web_bridge import WebBot
    from xgent_app.web_auth import hash_password
    from xgent_app.web_server import WebChatConfig, WebChatServer

    async with CompressionHarness(bot, root) as h:
        prepare(bot, h)
        tasks, stale = set(), []
        loop = asyncio.get_running_loop()
        validate = bot.validate_saved_menu_action

        def preview_action(action):
            if action not in {'act_main_menu', 'menu_more_settings', 'menu_skills'} and not action.startswith('set_skill_state:'):
                raise UiHistoryError('Preview: this operation is disabled.')
            validate(action)

        h.stack.enter_context(patch.object(bot, 'validate_saved_menu_action', preview_action))

        def disabled(outbox):
            outbox.put({'type': 'callback_answer', 'text': 'Preview: this operation is disabled.', 'show_alert': True})
            outbox.put({'type': 'turn_end'})

        async def setting(key, value):
            if key != 'skill_state':
                raise ValueError('Preview: this operation is disabled.')
            return await bot._web_write_setting(key, value)

        def schedule(coro):
            def start():
                task = loop.create_task(coro)
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            loop.call_soon_threadsafe(start)

        async def command(text, outbox):
            if text == '/fixture/delete':
                saved = (await menus(bot))[0]
                stale.append({**saved, 'type': 'message', 'text': saved['content']})
                row = await h.db.get_ui_message(saved['ui_message_id'])
                async with ui_operation(binding=row, message_id=99):
                    await WebBot(outbox, 1).delete_message(1, 99)
            elif text == '/fixture/replay':
                for frame in stale:
                    outbox.put(frame)
            elif text == '/fixture/clear':
                for saved in await menus(bot):
                    stale.append({**saved, 'type': 'message', 'text': saved['content']})
                await h.db.clear_all_conversation_memory()
                outbox.put({'type': 'history_reset'})
            elif text in {'/start', '/skills', '/config'}:
                await bot._web_handle_command(text, outbox)
            else:
                disabled(outbox)

        password = 'local-menu-fixture'
        server = WebChatServer(WebChatConfig(
            host='127.0.0.1', port=port, password_hash=hash_password(password),
            bot_token='', authorized_user_id=1, loop=loop,
            submit_message=lambda text, outbox: disabled(outbox),
            submit_command=lambda text, outbox: schedule(command(text, outbox)),
            submit_callback=lambda data, mid, outbox: disabled(outbox),
            submit_ui_callback=lambda uid, revision, bid, outbox: schedule(
                bot._web_handle_ui_callback(uid, revision, bid, outbox)),
            read_history=bot._web_read_history, read_history_message=bot._web_read_history_message,
            read_settings=bot._web_read_settings, write_setting=setting,
            request_stop=bot._web_request_stop, is_busy=bot._web_is_busy,
        ))
        server.outbox = h.outbox
        server.start()
        print(json.dumps({'url': f'http://127.0.0.1:{server._httpd.server_address[1]}',
                          'password': password}), flush=True)
        try:
            if forever:
                await asyncio.Event().wait()
            else:
                await asyncio.to_thread(sys.stdin.readline)
        finally:
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(server.stop)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args()
    with contextlib.ExitStack() as stack:
        root = args.root or Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='xgent-menu-qa-')))
        root.mkdir(parents=True, exist_ok=True)
        try:
            asyncio.run(serve(root, args.port, args.serve))
        except KeyboardInterrupt:
            pass
