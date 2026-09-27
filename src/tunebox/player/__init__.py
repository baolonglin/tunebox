"""Playback adapters: the host's side of PLAN.md Phase 3.

``owntone`` is the primary path; ``selection`` keeps the soundbar selected across restarts and
power-cycles. If OwnTone's Chromecast output ever has to be abandoned for fallback A, a
``cast.py`` module implementing the same :class:`~tunebox.core.ports.Player` protocol is the
whole of the change.
"""

from .owntone import OwnToneClient, OwnTonePlayer
from .selection import OutputKeeper

__all__ = ["OwnToneClient", "OwnTonePlayer", "OutputKeeper"]
