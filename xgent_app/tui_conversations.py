"""Small modal conversation manager for the existing prompt_toolkit application."""
from __future__ import annotations


class ConversationPanel:
    def __init__(self, app, body, hooks, restore_focus):
        from prompt_toolkit.widgets import TextArea
        self.app, self.body, self.hooks, self.restore_focus = app, body, hooks, restore_focus
        self.active = False
        self.archived = False
        self.index = 0
        self.busy = False
        self.error = ''
        self._float = None
        self._view = 'list'
        self.search = TextArea(height=1, multiline=False, prompt='搜索 ', name='conversation-search', focus_on_click=True)
        self.search.buffer.on_text_changed += self._filter_changed

    def _filter_changed(self, _):
        self.index = 0
        self.app().invalidate()

    def items(self):
        query = self.search.text.strip().casefold()
        return [row for row in self.hooks.conversation_items()
                if bool(row.get('archived')) == self.archived
                and query in str(row.get('name') or '新对话').casefold()]

    def selected(self):
        rows = self.items()
        self.index = max(0, min(self.index, len(rows) - 1))
        return rows[self.index] if rows else None

    def _mount(self, title, content, buttons, focus=None):
        from prompt_toolkit.widgets import Dialog
        from prompt_toolkit.layout.containers import Float, HSplit
        from prompt_toolkit.layout.dimension import D
        from prompt_toolkit.key_binding import KeyBindings
        kb = KeyBindings()
        @kb.add('escape')
        @kb.add('f4')
        def back(_):
            if self.busy:
                return
            if self._view != 'list':
                self.open()
            else:
                self.close()
        width = max(24, min(78, self.app().output.get_size().columns - 4))
        dialog = Dialog(title=title, body=content, buttons=buttons, width=D(preferred=width, max=width), modal=False)
        modal = HSplit([dialog], key_bindings=kb, modal=True)
        if self._float is not None:
            self.body().floats.remove(self._float)
        self._float = Float(content=modal)
        self.body().floats.append(self._float)
        self.active = True
        self.app().layout.focus(focus or dialog)
        self.app().invalidate()

    def close(self):
        if self.busy:
            return
        if self._float is not None:
            self.body().floats.remove(self._float)
            self._float = None
        self.active = False
        self.restore_focus()
        self.app().invalidate()

    def open(self):
        if self.busy or self.hooks.manage_conversation is None:
            return
        from prompt_toolkit.widgets import Button, Label
        from .cli_tui import fit_title
        from prompt_toolkit.layout import HSplit, VSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import D
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.mouse_events import MouseEventType
        self._view = 'list'
        self.error = ''
        kb = KeyBindings()
        @kb.add('up')
        def up(_):
            self.index = max(0, self.index - 1)
        @kb.add('down')
        def down(_):
            self.index = min(max(0, len(self.items()) - 1), self.index + 1)
        @kb.add('pageup')
        def previous(_):
            self.index = max(0, self.index - 8)
        @kb.add('pagedown')
        def next_page(_):
            self.index = min(max(0, len(self.items()) - 1), self.index + 8)
        @kb.add('enter')
        def choose(_):
            self.choose()
        @kb.add('f2')
        def rename(_):
            self.manage_selected()
        @kb.add('c-n')
        def create(_):
            self.perform('create', close=True)
        def click(index):
            def handle(event):
                if event.event_type == MouseEventType.MOUSE_UP:
                    self.index = index
                    self.app().layout.focus(control)
            return handle
        def text():
            rows = self.items()
            if not rows:
                return [('class:hint', '暂无匹配的对话')]
            selected = self.hooks.conversation().get('conversation_id')
            running = self.hooks.running() or {}
            done = self.hooks.completed()
            out = []
            from .cli_tui import fit_title
            width = max(12, min(70, self.app().output.get_size().columns - 12))
            for i, row in enumerate(rows):
                cid = row['id']
                suffix = ' · 运行中' if running.get('conversation_id') == cid else ' · 已结束' if cid in done else ''
                name = ('✓ ' if cid == selected else '  ') + str(row.get('name') or '新对话') + suffix
                out.append(('reverse' if i == self.index else '', fit_title(name, width), click(i)))
                if i < len(rows) - 1:
                    out.append(('', '\n'))
            return out
        control = FormattedTextControl(text, focusable=True, key_bindings=kb,
            get_cursor_position=lambda: Point(x=0, y=max(0, min(self.index, len(self.items()) - 1))))
        def search_accept(_):
            self.app().layout.focus(control)
            return True
        self.search.accept_handler = search_accept
        def toggle():
            self.archived = not self.archived
            self.index = 0
            self.open()
        contents = HSplit([
            self.search,
            Window(control, height=D(min=2, max=12), wrap_lines=False),
            Label(lambda: fit_title(
                self.error or '↑↓选择 · Enter打开 · Esc返回', max(12, self.app().output.get_size().columns - 12))),
            VSplit([Button('打开', self.choose, width=8), Button('新建', lambda: self.perform('create', close=True), width=8),
                    Button('管理', self.manage_selected, width=8)], padding=1),
        ], padding=1)
        self._mount('已归档对话' if self.archived else '对话记录', contents,
                    [Button('最近对话' if self.archived else '已归档', toggle), Button('返回', self.close)], control)

    def perform(self, action, cid=None, name=None, *, close=False):
        if self.busy:
            return
        self.busy = True
        self.error = '正在处理…'
        self.app().invalidate()
        async def work():
            try:
                await self.hooks.manage_conversation(action, cid, name)
                self.busy = False
                if close:
                    self.close()
                else:
                    self.open()
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.busy = False
                self.app().invalidate()
        self.app().create_background_task(work())

    def choose(self):
        row = self.selected()
        if row is None or self.busy:
            return
        if row.get('archived'):
            self.perform('restore_open', row['id'], close=True)
        else:
            self.perform('switch', row['id'], close=True)

    def manage_selected(self):
        row = self.selected()
        if row is None or self.busy:
            return
        from prompt_toolkit.widgets import Button, Label
        from prompt_toolkit.layout import HSplit, VSplit
        self._view = 'manage'
        contents = HSplit([
            Label(str(row.get('name') or '新对话') + ' · ' + row['id'][:6]),
            VSplit([Button('重命名', lambda: self.rename(row)),
                    Button('恢复' if row.get('archived') else '归档',
                           lambda: self.perform('restore' if row.get('archived') else 'archive', row['id']))], padding=1),
            VSplit([Button('重置上下文', lambda: self.confirm(row, False)),
                    Button('永久删除', lambda: self.confirm(row, True))], padding=1),
            Label(lambda: self.error),
        ], padding=1)
        self._mount('管理对话', contents, [Button('返回', self.open)])

    def rename(self, row):
        from prompt_toolkit.widgets import Button, Label, TextArea
        from prompt_toolkit.layout import HSplit
        from prompt_toolkit.filters import Condition
        self._view = 'rename'
        self.error = ''
        field = TextArea(text=str(row.get('name') or ''), height=1, multiline=False,
                         read_only=Condition(lambda: self.busy), name='conversation-name', focus_on_click=True)
        def save():
            self.perform('rename', row['id'], field.text)
        def rename_accept(_):
            save()
            return True
        field.accept_handler = rename_accept
        self._mount('重命名对话', HSplit([Label('名称（1–80 字）'), field, Label(lambda: self.error)], padding=1),
                    [Button('保存', save), Button('返回', self.open)], field)

    def confirm(self, row, deleting):
        if self.busy:
            return
        self.busy = True
        async def prepare():
            from prompt_toolkit.widgets import Button, Label
            from prompt_toolkit.layout import HSplit
            try:
                info = await self.hooks.manage_conversation('delete_info', row['id'])
                self._view = 'confirm'
                self.error = ''
                description = (f"永久删除全部历史，并终止 {info['active_tasks']} 个未完成任务。不可恢复。" if deleting
                               else '只重置模型上下文；历史、附件和任务仍保留。')
                self._mount('确认永久删除' if deleting else '确认重置上下文',
                    HSplit([Label(str(info['name']) + ' · ' + row['id'][:6]), Label(description), Label(lambda: self.error)], padding=1),
                    [Button('确认删除' if deleting else '确认重置',
                            lambda: self.perform('delete' if deleting else 'reset_context', row['id'])),
                     Button('取消', self.open)])
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.busy = False
                self.app().invalidate()
        self.app().create_background_task(prepare())
