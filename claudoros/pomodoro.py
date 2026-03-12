"""Passive focus tracker — derives work state purely from message timestamps."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

BREAK_DETECT_SECS  = 5 * 60    # idle ≥ 5 min  → on a natural break
BLOCK_GAP_SECS     = 30 * 60   # gap > 30 min  → new work block
BREAK_SUGGEST_SECS = 25 * 60   # progress bar fills at 25 min
BREAK_URGENT_SECS  = 40 * 60   # bar blinks
REST_ALARM_SECS    = 45 * 60   # rest alarm threshold
IDLE_SECS          = 60 * 60   # idle > 1 hr   → fully done for now


@dataclass
class FocusStatus:
    is_working: bool
    block_seconds: int         # seconds in current work block
    idle_seconds: int          # seconds since last user message
    suggest_break: bool
    urgent_break: bool
    block_start: Optional[datetime]
    last_msg_ts: Optional[datetime]
    # natural break (5 min – 1 hr of no activity)
    on_natural_break: bool = False
    natural_break_secs: int = 0
    # day stats
    pomodoros_done: int = 0        # completed blocks ≥ 25 min
    work_block_count: int = 0      # total blocks (any length)
    longest_streak_secs: int = 0   # longest run with all gaps < 5 min
    longest_break_secs: int = 0    # longest gap between messages (real break)

    @property
    def overtime_seconds(self) -> int:
        return max(0, self.block_seconds - BREAK_SUGGEST_SECS) if self.is_working else 0

    @property
    def progress(self) -> float:
        if not self.is_working:
            return 0.0
        return min(1.0, self.block_seconds / BREAK_SUGGEST_SECS)

    @property
    def status_color(self) -> str:
        if not self.is_working:
            return "overlay0"
        if self.urgent_break:
            return "red"
        if self.suggest_break:
            return "yellow"
        return "peach"

    @property
    def status_line(self) -> str:
        if self.on_natural_break:
            m, s = divmod(self.natural_break_secs, 60)
            return f"break {m:02d}:{s:02d}"
        if not self.is_working:
            return "waiting for activity…"
        m, s = divmod(self.block_seconds, 60)
        elapsed = f"{m:02d}:{s:02d}"
        if self.urgent_break:
            return f"{elapsed} — take a break!"
        if self.suggest_break:
            return f"{elapsed} — break time?"
        return f"{elapsed} focused"


def compute_focus(user_timestamps: list[datetime]) -> FocusStatus:
    """
    Pure function: given user message timestamps (today + recent sessions),
    return current focus state and today's stats.
    """
    now = datetime.now(timezone.utc)

    if not user_timestamps:
        return FocusStatus(
            is_working=False, block_seconds=0, idle_seconds=0,
            suggest_break=False, urgent_break=False,
            block_start=None, last_msg_ts=None,
        )

    tss = sorted(
        ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        for ts in user_timestamps
    )
    last_ts = tss[-1]
    idle_secs = int((now - last_ts).total_seconds())

    # ── gaps between consecutive messages ─────────────────────────────────
    gaps = [(tss[i + 1] - tss[i]).total_seconds() for i in range(len(tss) - 1)]

    # ── work blocks (gap > 30 min = new block) ────────────────────────────
    blocks: list[tuple[datetime, datetime]] = []
    blk_start = tss[0]
    for i, gap in enumerate(gaps):
        if gap > BLOCK_GAP_SECS:
            blocks.append((blk_start, tss[i]))
            blk_start = tss[i + 1]
    blocks.append((blk_start, last_ts))

    # ── longest streak (all gaps < 5 min) ─────────────────────────────────
    streak_start = tss[0]
    best_streak = 0.0
    for i, gap in enumerate(gaps):
        if gap >= BREAK_DETECT_SECS:
            best_streak = max(best_streak, (tss[i] - streak_start).total_seconds())
            streak_start = tss[i + 1]
    # final / current streak
    cur_streak_end = now if idle_secs < BREAK_DETECT_SECS else last_ts
    best_streak = max(best_streak, (cur_streak_end - streak_start).total_seconds())

    # ── longest break (gap ≥ 5 min, < 6 hr — exclude sleep) ──────────────
    SLEEP_SECS = 6 * 60 * 60
    break_gaps = [g for g in gaps if BREAK_DETECT_SECS <= g < SLEEP_SECS]
    if BREAK_DETECT_SECS <= idle_secs < SLEEP_SECS:
        break_gaps.append(float(idle_secs))
    longest_break = int(max(break_gaps, default=0))

    # ── pomodoros ─────────────────────────────────────────────────────────
    pomodoros = sum(
        1 for bs, be in blocks[:-1]
        if (be - bs).total_seconds() >= BREAK_SUGGEST_SECS
    )
    if blocks:
        bs, be = blocks[-1]
        if (be - bs).total_seconds() >= BREAK_SUGGEST_SECS and idle_secs >= BREAK_DETECT_SECS:
            pomodoros += 1

    day_kwargs = dict(
        pomodoros_done=pomodoros,
        work_block_count=len(blocks),
        longest_streak_secs=int(best_streak),
        longest_break_secs=longest_break,
    )

    # ── current state ──────────────────────────────────────────────────────
    if idle_secs > IDLE_SECS:
        return FocusStatus(
            is_working=False, block_seconds=0, idle_seconds=idle_secs,
            suggest_break=False, urgent_break=False,
            block_start=None, last_msg_ts=last_ts, **day_kwargs,
        )

    if idle_secs >= BREAK_DETECT_SECS:
        return FocusStatus(
            is_working=False, block_seconds=0, idle_seconds=idle_secs,
            suggest_break=False, urgent_break=False,
            block_start=None, last_msg_ts=last_ts,
            on_natural_break=True, natural_break_secs=idle_secs,
            **day_kwargs,
        )

    # ── current streak: time since last break (gap ≥ 5 min) ──────────────
    # streak_start is already updated above — points to the start of the
    # current continuous run.  Use THIS for the progress bar, not the
    # 30-min block start.
    streak_secs = int((now - streak_start).total_seconds())

    return FocusStatus(
        is_working=True,
        block_seconds=streak_secs,
        idle_seconds=idle_secs,
        suggest_break=streak_secs >= BREAK_SUGGEST_SECS,
        urgent_break=streak_secs >= BREAK_URGENT_SECS,
        block_start=streak_start,
        last_msg_ts=last_ts,
        **day_kwargs,
    )
