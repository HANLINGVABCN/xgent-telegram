"""Telegram 出口的 HTML 适配：只在真正打到 Bot API 的那一刻转换折叠块形态。

全链路只渲染一种「标准折叠形态」（``<blockquote expandable data-raw=…>`` 内含
``<pre>``），网页 / CLI / 历史回放 / 镜像帧都直接用它——多端与刷新前后因此一致。
Telegram 不认这种形态（blockquote 内嵌 pre 报 400、data-raw 非法属性），所以在
bot **类**上把 send_message / edit_message_text 包一层：parse_mode 为 HTML 时经
``ProtocolParser.to_telegram_html`` 转换，并仅在此裁剪块头及正文预览。
标准 HTML 保留全部协议内容，Web/TUI 的展开和复制不继承 Telegram 长度限制。

装在类上而不是散落在各调用点，是因为 Telegram 出口不止一条：直连会话的
msg.edit_text、网页→TG 的 fanout 投递（deliver_op_to_bot）、TG→网页镜像补丁
（install_tg_to_web_mirror 包在本适配外面：它先把**原文**推给网页，再调到这里
转成 Telegram 形态）。转换幂等，重复经过无害。
"""

from __future__ import annotations

from typing import Any

from xgent_app.protocols import ProtocolParser

_MARK = "_xgent_tg_html_adapted"


def _is_html(parse_mode: Any) -> bool:
    return parse_mode is not None and "html" in str(parse_mode).lower()


def adapt_call(kwargs: dict, args: tuple, text_pos: int) -> tuple:
    """把调用参数里的 text 转成 Telegram 形态（仅 HTML）。返回新的 (args, kwargs)。"""
    if not _is_html(kwargs.get("parse_mode")):
        return args, kwargs
    if "text" in kwargs:
        kwargs = dict(kwargs)
        kwargs["text"] = ProtocolParser.to_telegram_html(str(kwargs["text"]))
    elif len(args) > text_pos:
        args = list(args)
        args[text_pos] = ProtocolParser.to_telegram_html(str(args[text_pos]))
        args = tuple(args)
    return args, kwargs


def install_telegram_html_adapter(bot: Any) -> None:
    """在 bot 的类上装出口适配（幂等）。"""
    if bot is None:
        return
    cls = type(bot)
    if getattr(cls, _MARK, False):
        return
    orig_send = cls.send_message
    orig_edit = cls.edit_message_text

    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        args, kwargs = adapt_call(kwargs, args, 1)       # send_message(chat_id, text, …)
        return await orig_send(self, *args, **kwargs)

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> Any:
        args, kwargs = adapt_call(kwargs, args, 0)       # edit_message_text(text, …)
        return await orig_edit(self, *args, **kwargs)

    setattr(cls, "send_message", send_message)
    setattr(cls, "edit_message_text", edit_message_text)
    setattr(cls, _MARK, True)
