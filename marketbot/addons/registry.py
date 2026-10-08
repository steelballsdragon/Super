"""The add-on features the bot loads, in /help order. Each batch appends its features here."""

from __future__ import annotations

from .calendar import CalendarDesk
from .congress import CongressDesk
from .why import WhyDesk

FEATURES: list[type] = [WhyDesk, CongressDesk, CalendarDesk]
