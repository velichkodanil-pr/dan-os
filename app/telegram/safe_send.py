"""Telegram HTML sends that cannot be lost to a markup error.

The bot runs with parse_mode=HTML. One unescaped «<» from an email sender
(«Joyce Tours <receivable@joyce-tours.net>») made Telegram reject the whole
morning brief on 2026-11-22, and the ritual retried for six hours without ever
arriving. Builders escape external text at the source; this module is the net
under them: if Telegram still cannot parse the markup, the same message goes
out once more as plain text instead of not at all.
"""
import html as _html
import logging
import re

from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)

# Only the tags Telegram's HTML mode knows. Anything else in angle brackets
# (an email address, «<Юра>») is CONTENT and must survive the fallback.
_TG_TAG = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|span|tg-spoiler|"
    r"tg-emoji|blockquote)(?:\s[^<>]*)?>", re.IGNORECASE)


def plain_from_html(text: str) -> str:
    """Telegram HTML -> readable plain text: our tags removed, entities decoded,
    every other character kept as is."""
    return _html.unescape(_TG_TAG.sub("", text or ""))


def is_markup_error(exc: Exception) -> bool:
    """Telegram's «can't parse entities» — the markup, not the message, is bad."""
    return "can't parse entities" in str(exc).lower()


async def send_html(bot, chat_id: int, text: str, **kwargs):
    """send_message with HTML; on a markup rejection, resend once as plain text.

    Any other error propagates unchanged, so callers keep their own retry and
    alert logic. The log line never carries the message body."""
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramBadRequest as exc:
        if not is_markup_error(exc):
            raise
        logger.warning("telegram rejected HTML markup; resending as plain text")
        kwargs.pop("parse_mode", None)
        return await bot.send_message(chat_id, plain_from_html(text),
                                      parse_mode=None, **kwargs)
