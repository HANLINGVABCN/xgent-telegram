"""Isolated Web UI fixture. No application database, providers, or Telegram calls."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw

from xgent_app import web_auth
from xgent_app.web_bridge import WebBot
from xgent_app.web_history import build_history_message, display_media_reference
from xgent_app.web_server import WebChatConfig, WebChatServer
from xgent_app.web_media import build_media_presentation, media_presentation_scope


RICH_TEXT = """## Refresh verification

| Item | State | Count |
|:-----|:-----:|------:|
| **Original images** | Ready | 2 |
| Export downloads | Ready | 3 |

> A quoted message.
> Its second line stays in the same block.

- [x] Saved originals
- [ ] Next step

```python
def verify():
    return "original content"
```

Search literal: `<img src=x onerror=alert(1)>`
"""


class Fixture:
    def __init__(self, root: Path, loop: asyncio.AbstractEventLoop):
        self.root, self.loop = root, loop
        self.records = {}
        self.serial = 0
        self.busy = False
        self.stopped = False
        self.tasks = set()
        self.restore_notice = None
        self.restore_gate = None
        self.setting_values = {'disabled_skills': [], 'hidden_skills': []}
        self.populate()

    def add(self, kind, content, media=None, metadata=None):
        self.serial += 1
        row = {
            "id": self.serial, "timestamp": 1700000000 + self.serial,
            "role": "user" if kind.startswith("user_") else "assistant",
            "msg_type": kind, "content": content, "metadata": dict(metadata or {}),
        }
        if media is not None:
            row["metadata"]["display_media"] = [
                display_media_reference(str(path), name) for path, name in media
            ]
        self.records[self.serial] = row
        return row

    def populate(self):
        images = []
        for number, color in ((1, "#cc4455"), (2, "#208d77")):
            path = self.root / f"original image {number}.png"
            image = Image.new("RGB", (480, 280), "#f5f7fa")
            draw = ImageDraw.Draw(image)
            draw.rectangle((36, 42, 225, 225), fill=color)
            draw.text((260, 120), f"ORIGINAL {number}", fill="#222222")
            image.save(path)
            images.append((path, f"original {number}.png"))
        report = self.root / "Token report.html"
        report.write_text("<!doctype html><html><body>Fixture report</body></html>", encoding="utf-8")
        export = self.root / "memory export.zip"
        with zipfile.ZipFile(export, "w") as archive:
            archive.writestr("memory.txt", "original exported memory")
        text = self.root / "requirements with spaces and a deliberately long filename for mobile.txt"
        text.write_text("original text\nEND OF ORIGINAL TEXT", encoding="utf-8")
        self.add("user_text", "Verify this conversation after refresh.")
        self.add("ai_reply", RICH_TEXT)
        self.add("user_photo", "Two originals, in upload order.\nThe complete caption stays visible.", images)
        self.add("ai_reply", "Generated result and original file.", [images[1]])
        self.add("system_op", "Saved exports and text file.", [
            (export, "\u7cfb\u7edf\u8bb0\u5fc6.zip"), (report, report.name), (text, text.name),
        ])
        self.add("user_file", "This original has intentionally been removed.", [
            (self.root / "missing.txt", "missing.txt"),
        ])
        self.add("agent_result", "model-visible shell notice", metadata={
            "display": {"content": "<b>Agent Shell</b>\n<pre>first\n  second\nthird</pre>",
                        "parse_mode": "HTML"},
        })
        self.add("token_usage", "<i>Model: fixture | Tokens: 123</i>")

    async def history(self, limit):
        return [build_history_message(row, self.root, self.root)
                for row in list(self.records.values())[-limit:]]

    async def message(self, row_id):
        row = self.records.get(row_id)
        return build_history_message(row, self.root, self.root) if row else None

    async def settings(self, *args):
        if args:
            key, value = args
            if key == 'skill_state':
                path, state = value['path'], value['state']
                disabled = set(self.setting_values['disabled_skills'])
                hidden = set(self.setting_values['hidden_skills'])
                if state == 'hidden':
                    hidden.add(path)
                else:
                    hidden.discard(path)
                    if state == 'disabled':
                        disabled.add(path)
                    else:
                        disabled.discard(path)
                self.setting_values.update(disabled_skills=sorted(disabled), hidden_skills=sorted(hidden))
            else:
                self.setting_values[key] = value
        return {"values": dict(self.setting_values), "options": {"skill_list": [
            {"path": "daily.md", "label": "Daily", "source": "public"},
            {"path": "private/long.md", "label": "A deliberately long skill name for mobile layout verification", "source": "private"},
            {"path": "private/\u4e2d\u6587\u8def\u5f84.md", "label": "\u4e2d\u6587\u6280\u80fd\u6587\u4ef6\u540d\u4e0e\u8def\u5f84\u6d4b\u8bd5", "source": "private"},
        ]}}

    def schedule(self, coroutine):
        def start():
            task = self.loop.create_task(coroutine)
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        self.loop.call_soon_threadsafe(start)

    def submit_message(self, text, outbox):
        self.busy = True
        self.schedule(self.chat(text, outbox))

    async def chat(self, text, outbox):
        self.add("user_text", text)
        bot = WebBot(outbox, 1)
        message = await bot.send_message(1, "Streaming fixture...")
        await asyncio.sleep(0.2)
        answer = "## LIVE FINAL\n\n" + RICH_TEXT
        self.add("ai_reply", answer)
        await message.edit_text(answer)
        self.busy = False
        outbox.put({"type": "turn_end"})

    def submit_command(self, command, outbox):
        if command in ("/fixture/busy", "/fixture/error", "/fixture/compression", "/fixture/compression-error",
                       "/fixture/compression-hold-error", "/fixture/progress", "/fixture/generated"):
            self.busy = True
        self.schedule(self.command(command, outbox))

    def submit_callback(self, data, message_id, outbox):
        if (data != 'retry_compress:' + 'a' * 32 or self.restore_notice is None
                or message_id != self.restore_notice['id'] or message_id not in self.records):
            outbox.put({'type': 'callback_answer', 'text': 'Invalid restore callback', 'show_alert': True})
            return
        self.busy = True
        self.schedule(self.restore(outbox, retry=True))

    async def restore(self, outbox, *, retry=False, failed=False, hold=False):
        outbox.put({'type': 'compression_state', 'busy': True})
        bot = WebBot(outbox, 1)
        if not retry:
            await bot.send_message(1, 'EXPORTING FIXTURE')
            await asyncio.sleep(0.7)
            self.records.clear()
            self.restore_notice = self.add('system_op', 'RESTORING FIXTURE', [
                (self.root / 'memory export.zip', 'archive.zip'),
            ], metadata={'compression_task': {'id': 'a' * 32, 'status': 'running'}})
            outbox.put({'type': 'history_reset'})
        else:
            self.restore_notice['content'] = 'RESTORING FIXTURE'
            self.restore_notice['metadata']['compression_task']['status'] = 'running'
        draft = await bot.send_message(1, 'RESTORING FIXTURE')
        if hold:
            self.restore_gate = asyncio.Event()
            await self.restore_gate.wait()
            self.restore_gate = None
        else:
            await asyncio.sleep(2.5)
        row = self.restore_notice
        if failed:
            row['content'] = 'COMPRESSION FAILED: archive retained; retry available'
            row['metadata']['compression_task']['status'] = 'failed'
            restored = build_history_message(row, self.root, self.root)
            outbox.put({'type': 'edit', 'message_id': draft.message_id, 'record_id': row['id'],
                        'text': row['content'], 'reply_markup': restored['reply_markup']})
        else:
            self.add('ai_reply', '## RESTORED CONVERSATION\n\nCOMPRESSION SUMMARY')
            await draft.edit_text('## RESTORED CONVERSATION\n\nCOMPRESSION SUMMARY')
            row['content'] = 'COMPRESSED FIXTURE'
            row['metadata']['compression_task']['status'] = 'completed'
        self.busy = False
        outbox.put({'type': 'compression_state', 'busy': False, 'committed': True})

    async def command(self, command, outbox):
        if command == '/fixture/generated':
            bot = WebBot(outbox, 1)
            draft = await bot.send_message(1, 'Generating fixture media...')
            artifacts = []
            for number, color in ((1, '#c84b56'), (2, '#168b74')):
                path = self.root / f'generated-{self.serial}-{number}.png'
                image = Image.new('RGB', (480, 280), color)
                ImageDraw.Draw(image).text((40, 40), f'GENERATED {number}', fill='white')
                image.save(path)
                artifacts.append({'path': str(path), 'mime_type': 'image/png'})
            paths = '\n\n'.join(f"[Saved original: {Path(item['path']).as_posix()}]" for item in artifacts)
            text = '## GENERATED REPLY\n\n**Two original images**, one complete reply.\n\n' + paths
            self.add('ai_reply', text, [(Path(item['path']), Path(item['path']).name) for item in artifacts])
            with media_presentation_scope(build_media_presentation(
                text, artifacts, replace_message_ids=[draft.message_id],
            )):
                for item in artifacts:
                    with open(item['path'], 'rb') as photo:
                        await bot.send_photo(1, photo, caption='Short Telegram caption')
                    await asyncio.sleep(2)
            await draft.delete()
            self.busy = False
        elif command == '/fixture/progress':
            self.stopped = False
            bot = WebBot(outbox, 1)
            draft = await bot.send_message(1, 'STREAM DRAFT 0')
            for step in range(1, 11):
                if self.stopped:
                    break
                text = f'COMMAND STEP {step}'
                self.add('agent_result', text)
                await bot.send_message(1, text)
                await draft.edit_text(f'STREAM DRAFT {step}')
                await asyncio.sleep(1)
            answer = 'PROGRESS STOPPED' if self.stopped else 'PROGRESS COMPLETE'
            self.add('ai_reply', answer)
            await draft.edit_text(answer)
            path = self.root / 'memory export.zip'
            self.add('system_op', 'PROGRESS ARCHIVE', [(path, 'progress.zip')])
            with path.open('rb') as file:
                await bot.send_document(1, file, filename='progress.zip')
            self.busy = False
        elif command in {'/fixture/compression-error', '/fixture/compression', '/fixture/compression-hold-error'}:
            await self.restore(outbox, failed=command.endswith('-error'), hold='-hold-' in command)
            return
        elif command == '/fixture/release-restore':
            if self.restore_gate is not None:
                self.restore_gate.set()
            return
        elif command in ("/fixture/busy", "/fixture/error"):
            self.stopped = False
            await asyncio.sleep(2)
            if command.endswith("error"):
                self.busy = False
                outbox.put({"type": "turn_error", "text": "Fixture interrupted error"})
                return
            answer = "Stopped fixture result" if self.stopped else "BUSY FINAL"
            self.add("ai_reply", answer)
            outbox.put({"type": "message", "message_id": 700, "text": answer})
            self.busy = False
        elif command == "/fixture/frames":
            for text in ("LIVE UPDATE", "LIVE UPDATE"):
                outbox.put({"type": "message", "message_id": 801, "text": text})
            outbox.put({"type": "edit", "message_id": 801, "text": "UPDATED FINAL"})
            outbox.put({"type": "delete", "message_id": 802})
            outbox.put({"type": "message", "message_id": 803, "text": "DELETE ME"})
            outbox.put({"type": "delete", "message_id": 803})
            outbox.put({
                "type": "message", "message_id": 804, "text": "URL button",
                "reply_markup": [[{"text": "Example", "url": "https://example.com"}]],
            })
        elif command == "/fixture/clear":
            self.records.clear()
            outbox.put({"type": "history_reset"})
        outbox.put({"type": "turn_end"})

    def submit_upload(self, filename, data, caption, outbox):
        self.busy = True
        self.stopped = False
        self.schedule(self.upload(filename, data, caption, outbox))

    async def upload(self, filename, data, caption, outbox):
        path = self.root / Path(filename).name
        path.write_bytes(data)
        self.add("user_file", caption or filename, [(path, filename)])
        await asyncio.sleep(1 if filename.startswith("slow-") else 0.2)
        if filename == "queue-error.txt":
            self.busy = False
            outbox.put({"type": "turn_error", "text": "Fixture upload turn failed"})
            return
        answer = f"{'Stopped' if self.stopped else 'Received'} {filename}"
        self.add("ai_reply", answer)
        outbox.put({"type": "message", "message_id": self.serial + 10000, "text": answer})
        self.busy = False
        outbox.put({"type": "turn_end"})

    def stop(self):
        self.stopped = True


async def main(port, serve):
    with tempfile.TemporaryDirectory(prefix="xgent-web-fixture-") as directory:
        fixture = Fixture(Path(directory).resolve(), asyncio.get_running_loop())
        password = "local-web-fixture"
        server = WebChatServer(WebChatConfig(
            host="127.0.0.1", port=port, password_hash=web_auth.hash_password(password),
            bot_token="", authorized_user_id=1, loop=fixture.loop,
            submit_message=fixture.submit_message, submit_command=fixture.submit_command,
            submit_callback=fixture.submit_callback,
            submit_upload=fixture.submit_upload, read_history=fixture.history,
            read_history_message=fixture.message, read_settings=fixture.settings,
            write_setting=fixture.settings, request_stop=fixture.stop,
            is_busy=lambda: fixture.busy,
        ))
        server.start()
        print(json.dumps({
            "url": f"http://127.0.0.1:{server._httpd.server_address[1]}",
            "password": password,
        }), flush=True)
        try:
            if serve:
                await asyncio.Event().wait()
            else:
                await asyncio.to_thread(sys.stdin.readline)
        finally:
            for task in list(fixture.tasks):
                task.cancel()
            await asyncio.gather(*fixture.tasks, return_exceptions=True)
            await asyncio.to_thread(server.stop)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--serve", action="store_true", help="Run until interrupted instead of waiting for stdin.")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.port, args.serve))
    except KeyboardInterrupt:
        pass
