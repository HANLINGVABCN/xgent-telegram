"""Real prompt_toolkit + xgent_cli entrypoints, isolated SQLite and simulated engine."""
import asyncio
import os
from pathlib import Path
from unittest.mock import patch


async def actual_tui(cli, scenario='routing'):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.data_structures import Size
    from xgent_app.cli_bridge import set_screen
    from xgent_app.cli_tui import PtScreen, run_tui
    from xgent_app.cli_render import Palette
    from xgent_app.conversations import bind_conversation, current_scope
    from xgent_app.interaction import interaction

    class Output(DummyOutput):
        size = Size(rows=28, columns=90)
        def get_size(self):
            return self.size
    screen = PtScreen(Palette(False), width=89)
    cli.SCREEN, cli.PALETTE = screen, screen.palette
    set_screen(screen)
    os.environ['XGENT_CLI_NO_TG_MIRROR'] = '1'
    await cli._init_runtime()
    manager = cli.get_conversations()
    db = await cli.BotMemoryDB.get_instance()
    work = cli._terminal_workspace
    try:
        with interaction('cli'):
            a = (await manager.state())['terminal_conversation_id']
            await manager.manage('rename', a, '终端 A', selector='terminal')
            b = (await manager.manage('create', name='终端 B', selector='terminal'))['conversation_id']
            await manager.manage('switch', a, selector='terminal')
        for cid, text in ((a,'A 的独立历史'), (b,'B 的独立历史')):
            with bind_conversation(await manager.resolve(cid, selector='terminal')):
                await cli.GlobalRecorder.record_user_message(text)
        await db.set_config('default_model','gemini-3.8-flash-high')
        await work.refresh_state()
        await work.refresh_history(force=True)
        entered = asyncio.Event()
        finish = asyncio.Event()
        calls = []
        async def engine(update, context, text):
            calls.append((current_scope().conversation_id, text))
            await context.bot.send_message(1, '本机流式开始')
            entered.set()
            await finish.wait()
            await context.bot.send_message(1, '本机完整回复')
            await cli.GlobalRecorder.record_ai_reply('本机完整回复', 1)
        with patch.object(cli, 'process_conversation', engine):
            output = Output()
            with create_pipe_input() as inp, create_app_session(input=inp, output=output):
                task = asyncio.create_task(run_tui(screen, cli._build_tui_hooks()))
                await asyncio.sleep(.35)
                app = get_app()
                buf = app.current_buffer
                async def keys(value, delay=.2):
                    inp.send_text(value)
                    await asyncio.sleep(delay)
                async def wait_for(predicate, timeout=6):
                    for _ in range(int(timeout/.05)):
                        if predicate():
                            return
                        await asyncio.sleep(.05)
                    raise AssertionError('Timed out: ' + str(screen.model.render_rows())[:500])
                try:
                    assert '[' not in cli._prompt_plain()
                    if scenario == 'routing':
                        await keys('A 草稿')
                        await db.manage_conversation('switch', b)
                        await db.manage_conversation('switch', b, selector='telegram')
                        await manager.poll_once()
                        assert work.selected == a and buf.text == 'A 草稿'
                        await work.manage('switch', b)
                        assert buf.text == ''
                        await keys('B 草稿')
                        await work.manage('switch', a)
                        assert buf.text == 'A 草稿'
                        assert 'A 的独立历史' in str(screen.model.render_rows())
                        assert 'B 的独立历史' not in str(screen.model.render_rows())
                        # Acceptance clears only the admitted draft, not a background view.
                        await keys('\r')
                        await asyncio.wait_for(entered.wait(),5)
                        assert calls == [(a,'A 草稿')]
                        assert buf.text == ''
                        await work.manage('switch', b)
                        assert buf.text == 'B 草稿'
                        await keys('\r')
                        assert len(calls) == 1 and buf.text == 'B 草稿'
                        assert '本机流式开始' not in str(screen.model.render_rows())
                        # F4 and Enter are real modal navigation, allowed while generating.
                        await keys('\x1bOS', .4)  # F4
                        assert app.current_buffer is not buf
                        await keys('\x1b', .7)
                        assert app.current_buffer is buf
                        await keys('\x01\x0b/new 执行中新建\r', .5)
                        assert work.selected not in {a,b}
                        assert buf.text == '' and len(calls) == 1
                        assert (await db.get_conversation_state())['current_chat_id'] == b
                        finish.set()
                        await wait_for(lambda: not cli._turn_active)
                        await work.refresh_history(force=True)
                        assert '本机完整回复' not in str(screen.model.render_rows())
                        await work.manage('switch', a)
                        await work.refresh_history(force=True)
                        content = str(screen.model.render_rows())
                        assert content.count('本机完整回复') == 1, content
                        assert '本机流式开始' not in content
                        assert b == (await db.get_conversation_state())['current_chat_id']
                        assert b == (await db.get_conversation_state())['telegram_conversation_id']
                    elif scenario == 'stale':
                        await keys('旧上下文草稿')
                        with bind_conversation(await manager.resolve(a, selector='terminal')):
                            await db.clear_all_conversation_memory()
                        await work.refresh_state()
                        await keys('\r')
                        assert not calls and buf.text == '旧上下文草稿'
                        await wait_for(lambda: not cli._turn_active)
                        finish.set()
                        await keys('\r')
                        await wait_for(lambda: len(calls)==1)
                        assert calls[0] == (a,'旧上下文草稿')
                        await wait_for(lambda: not cli._turn_active)
                        # Removal never sends an existing draft to a fallback conversation.
                        await keys('删除前草稿')
                        await db.manage_conversation('archive', a)
                        await manager.poll_once()
                        await work.refresh_state()
                        assert work.selected is None
                        await keys('不能发送\r')
                        assert len(calls)==1 and buf.text == '不能发送'
                    elif scenario == 'write_failure':
                        from unittest.mock import AsyncMock
                        await keys('失败时保留的草稿')
                        with patch.object(cli.GlobalRecorder, 'record_user_message', AsyncMock(return_value=None)):
                            await keys('\r', .5)
                        assert not calls and buf.text == '失败时保留的草稿'
                        assert '失败时保留的草稿' not in str(screen.model.render_rows())
                        finish.set()
                        await keys('\r', .5)
                        await wait_for(lambda: len(calls)==1)
                        assert buf.text == ''
                        await wait_for(lambda: not cli._turn_active)
                    elif scenario == 'management':
                        from prompt_toolkit.formatted_text import to_formatted_text
                        async def click(label):
                            for control in app.layout.find_all_controls():
                                value = getattr(control, 'text', '')
                                try:
                                    text = ''.join(part[1] for part in to_formatted_text(value))
                                except Exception:
                                    continue
                                if text.strip().strip('<>').strip() == label:
                                    app.layout.focus(control)
                                    await keys('\r', .4)
                                    return
                            raise AssertionError('Button not found: ' + label)
                        await keys('管理时保留草稿')
                        await keys('\x1bOS', .3)
                        await keys('\x1b[Z', .2)
                        await keys('终端 B\r', .2)
                        await click('管理')
                        await click('重命名')
                        assert app.current_buffer.name == 'conversation-name'
                        await keys('\x01\x0b\r', .3)
                        assert app.current_buffer.name == 'conversation-name' and app.current_buffer.text == ''
                        await keys('已改名的终端 B\r', .5)
                        assert (await db.get_session(b))['name'] == '已改名的终端 B'
                        assert work.selected == a
                        await click('管理')
                        await click('重置上下文')
                        generation = (await db.get_session(b))['generation']
                        await click('确认重置')
                        assert (await db.get_session(b))['generation'] != generation
                        with bind_conversation(await manager.resolve(b, selector='terminal')):
                            assert 'B 的独立历史' in str(await db.get_display_history(0))
                            assert not await db.get_conversation_messages()
                        await click('管理')
                        await click('归档')
                        assert (await db.get_session(b))['archived']
                        await click('已归档')
                        await click('管理')
                        await click('永久删除')
                        assert await db.get_session(b) is not None
                        await click('确认删除')
                        await wait_for(lambda: all(row['id'] != b for row in work.items))
                        assert await db.get_session(b) is None
                        await click('返回')
                        assert app.current_buffer is buf and buf.text == '管理时保留草稿'
                        assert not calls
                    elif scenario == 'visual':
                        from PIL import Image, ImageDraw, ImageFont
                        folder = Path(__file__).resolve().parents[1] / 'workspace/tui-workspace-qa/screenshots'
                        folder.mkdir(parents=True, exist_ok=True)
                        font_path = Path('C:/Windows/Fonts/msyh.ttc')
                        if not font_path.exists():
                            font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf')
                        font = ImageFont.truetype(str(font_path), 16)
                        async def capture(label, width, height):
                            output.size = Size(rows=height, columns=width)
                            screen._forced_width = width-1
                            app.invalidate()
                            await asyncio.sleep(.35)
                            rendered = app.renderer._last_screen
                            image = Image.new('RGB', (width*10, height*23), '#151a22')
                            draw = ImageDraw.Draw(image)
                            text = []
                            for y in range(height):
                                text.append(''.join(rendered.data_buffer[y][x].char for x in range(width)))
                                for x in range(width):
                                    cell = rendered.data_buffer[y][x]
                                    if cell.char:
                                        color = '#64b5ef' if 'title.name' in cell.style else '#98a5b6' if 'title' in cell.style or 'hint' in cell.style else '#e3e8ef'
                                        if 'reverse' in cell.style:
                                            draw.rectangle((x*10,y*23,(x+max(1,cell.width))*10,(y+1)*23), fill='#384a61')
                                        draw.text((x*10,y*23),cell.char,font=font,fill=color)
                            image.save(folder/f'{width}-{label}.png')
                            (folder/f'{width}-{label}.txt').write_text('\n'.join(text),encoding='utf-8')
                            assert 'Window too small' not in '\n'.join(text), text
                        await keys('输入框不再挤占会话名')
                        for width,height in ((90,28),(44,24)):
                            await capture('chat',width,height)
                            await keys('\x1bOS', .2)
                            await capture('conversations',width,height)
                            await keys('\x1b', .7)
                        assert buf.text == '输入框不再挤占会话名'
                    elif scenario == 'panel':
                        await keys('不要清掉我的草稿')
                        await keys('\x1bOS', .4)
                        # Dialog's first focused control is the conversation list.
                        await keys('\x1b[Z', .2)
                        assert app.current_buffer.name == 'conversation-search'
                        await keys('终端 B\r', .2)
                        await keys('\r', .6)
                        await wait_for(lambda: work.selected == b)
                        assert buf.text == ''
                        await keys('\x1bOS', .3)
                        await keys('\x1b', .7)
                        assert app.current_buffer is buf
                        await work.manage('switch', a)
                        assert buf.text == '不要清掉我的草稿'
                    return {'scenario':scenario,'passed':True}
                finally:
                    finish.set()
                    await asyncio.sleep(.2)
                    if not task.done():
                        app.exit()
                    await asyncio.wait_for(task, 5)
    finally:
        await cli._shutdown_runtime()
