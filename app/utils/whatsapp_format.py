# savoury-spud-backend/app/utils/whatsapp_format.py
#
# WhatsApp does NOT render standard Markdown. It has its own, much smaller
# set: *bold* (single asterisks), _italic_, ~strikethrough~, and ```mono```.
# Anything else — **double-asterisk bold**, ## headers, [links](url) — just
# shows up as literal punctuation in the chat, which is exactly the "weird
# asterisks in between messages" bug from a real transcript review: the LLM
# was taught on standard Markdown and reasonably defaults to **bold**,
# which WhatsApp has no idea what to do with.
#
# Applied once, at the universal send boundary in whatsapp.py — every
# outbound message gets normalized regardless of whether it came from the
# LLM, a deterministic override, or hand-written Python, rather than
# hoping every call site (and every future one) remembers to sanitize
# itself, or hoping the LLM always perfectly follows a prompt instruction.

import re


def to_whatsapp_markdown(text: str) -> str:
    if not text:
        return text

    # **bold** or __bold__ -> *bold* (WhatsApp's actual bold syntax)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)

    # Markdown headers (# / ## / ### Title) -> just the bolded text, no hashes
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # [link text](url) -> "link text: url" — WhatsApp doesn't render markdown links at all
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r"\1: \2", text)

    return text
