"""Keeps every embed within Discord's limits, so a long post is trimmed instead of rejected.

Discord refuses a whole message if any part is too long (a field over 1024 characters, a description over
4096, or 6000 characters in total). Text is cut at a line break where possible and marked with "…".
"""

from __future__ import annotations

import discord

TITLE, DESCRIPTION, FIELD_NAME, FIELD_VALUE, FOOTER, FIELDS, TOTAL = 256, 4096, 256, 1024, 2048, 25, 6000
MESSAGE = 2000  # plain message text


def clip(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    cut = text[: limit - 1]
    newline = cut.rfind("\n")
    if newline > limit // 2:
        cut = cut[:newline]
    return cut + "…"


def fit_embed(embed: discord.Embed | None) -> discord.Embed | None:
    if embed is None:
        return None
    embed.title = clip(embed.title, TITLE)
    embed.description = clip(embed.description, DESCRIPTION)
    fields = [(clip(f.name, FIELD_NAME) or "​", clip(f.value, FIELD_VALUE) or "​", f.inline)
              for f in embed.fields[:FIELDS]]
    embed.clear_fields()
    for name, value, inline in fields:
        embed.add_field(name=name, value=value, inline=inline)
    if embed.footer and embed.footer.text:
        embed.set_footer(text=clip(embed.footer.text, FOOTER), icon_url=embed.footer.icon_url)
    # Still too long in total: trim the longest part until it fits.
    while len(embed) > TOTAL:
        excess = len(embed) - TOTAL
        parts = [("description", len(embed.description or ""))] + [(i, len(f.value)) for i, f in enumerate(embed.fields)]
        which, size = max(parts, key=lambda p: p[1])
        new_size = max(size - excess - 1, 20)
        if which == "description":
            embed.description = clip(embed.description, new_size)
        else:
            f = embed.fields[which]
            embed.set_field_at(which, name=f.name, value=clip(f.value, new_size), inline=f.inline)
        if new_size == 20:
            break
    return embed
