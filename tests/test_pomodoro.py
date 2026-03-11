"""Comprehensive tests for claudoros.pomodoro.compute_focus."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claudoros.pomodoro import (
    BREAK_DETECT_SECS,
    BREAK_SUGGEST_SECS,
    BREAK_URGENT_SECS,
    BLOCK_GAP_SECS,
    IDLE_SECS,
    compute_focus,
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── 1. Empty list ─────────────────────────────────────────────────────────────

def test_empty_list():
    f = compute_focus([])
    assert f.is_working is False
    assert f.block_seconds == 0
    assert f.idle_seconds == 0
    assert f.suggest_break is False
    assert f.urgent_break is False
    assert f.block_start is None
    assert f.last_msg_ts is None
    assert f.pomodoros_done == 0
    assert f.work_block_count == 0
    assert f.longest_streak_secs == 0
    assert f.longest_break_secs == 0


# ── 2. Single timestamp — just now → working, ~0 seconds ─────────────────────

def test_single_timestamp_just_now():
    ts = now_utc() - timedelta(seconds=5)
    f = compute_focus([ts])
    assert f.is_working is True
    assert f.block_seconds < 30  # approximately 0–10 seconds
    assert f.suggest_break is False
    assert f.urgent_break is False


# ── 3. Single timestamp — 2 hours ago → idle (> IDLE_SECS) ──────────────────

def test_single_timestamp_two_hours_ago():
    ts = now_utc() - timedelta(hours=2)
    f = compute_focus([ts])
    assert f.is_working is False
    assert f.idle_seconds > IDLE_SECS


# ── 4. Normal work session < 25 min → is_working, no suggest_break ───────────
# NOTE: block_seconds = streak seconds (time since last gap >= BREAK_DETECT_SECS=5min).
# To keep the streak intact, gaps between messages must be < 5 min.

def test_work_session_under_25_min():
    now = now_utc()
    # 5 messages with <5-min gaps, last one just now, spanning ~20 min total
    timestamps = [
        now - timedelta(minutes=20),
        now - timedelta(minutes=16),   # 4-min gap (< 5 min, streak intact)
        now - timedelta(minutes=12),   # 4-min gap
        now - timedelta(minutes=8),    # 4-min gap
        now - timedelta(minutes=4),    # 4-min gap
    ]
    f = compute_focus(timestamps)
    assert f.is_working is True
    assert f.suggest_break is False
    assert f.urgent_break is False
    # streak started from the first message (no gap >= 5 min),
    # block_seconds ≈ 20 min (time from tss[0] to now, since idle < 5 min)
    assert f.block_seconds >= 19 * 60
    assert f.block_seconds <= 22 * 60


# ── 5. Work session > 25 min → suggest_break ─────────────────────────────────

def test_work_session_over_25_min():
    now = now_utc()
    # Messages with <5-min gaps spanning 30 min; last message very recent
    timestamps = [
        now - timedelta(minutes=30),
        now - timedelta(minutes=26),   # 4-min gap
        now - timedelta(minutes=22),
        now - timedelta(minutes=18),
        now - timedelta(minutes=14),
        now - timedelta(minutes=10),
        now - timedelta(minutes=6),
        now - timedelta(minutes=2),    # 4-min gap, last msg 2 min ago
    ]
    f = compute_focus(timestamps)
    assert f.is_working is True
    # streak_secs ≈ 30 min (from first msg to now, all gaps < 5 min)
    assert f.suggest_break is True
    assert f.urgent_break is False


# ── 6. Work session > 40 min → urgent_break ──────────────────────────────────

def test_work_session_over_40_min():
    now = now_utc()
    # Dense messages with <5-min gaps spanning 45 min
    timestamps = [
        now - timedelta(minutes=45),
        now - timedelta(minutes=41),
        now - timedelta(minutes=37),
        now - timedelta(minutes=33),
        now - timedelta(minutes=29),
        now - timedelta(minutes=25),
        now - timedelta(minutes=21),
        now - timedelta(minutes=17),
        now - timedelta(minutes=13),
        now - timedelta(minutes=9),
        now - timedelta(minutes=5),
        now - timedelta(minutes=1),
    ]
    f = compute_focus(timestamps)
    assert f.is_working is True
    assert f.suggest_break is True
    assert f.urgent_break is True


# ── 7. Natural break: last message 10 min ago → on_natural_break ─────────────

def test_natural_break():
    now = now_utc()
    # Recent messages but last one 10 min ago → natural break
    timestamps = [
        now - timedelta(minutes=30),
        now - timedelta(minutes=26),
        now - timedelta(minutes=10),  # last msg 10 min ago (> BREAK_DETECT_SECS=5min)
    ]
    f = compute_focus(timestamps)
    assert f.is_working is False
    assert f.on_natural_break is True
    assert f.natural_break_secs >= BREAK_DETECT_SECS
    assert f.natural_break_secs < IDLE_SECS


# ── 8. Two messages with 35-min gap → two work blocks (work_block_count=2) ───

def test_two_work_blocks():
    now = now_utc()
    # Block 1: 65 and 55 min ago (gap within block = 10 min, < BLOCK_GAP_SECS=30min)
    # 35-min gap between blocks (> BLOCK_GAP_SECS)
    # Block 2: 20 and 10 min ago
    timestamps = [
        now - timedelta(minutes=65),
        now - timedelta(minutes=55),
        now - timedelta(minutes=20),  # 35-min gap before this (> 30-min BLOCK_GAP_SECS)
        now - timedelta(minutes=10),
    ]
    f = compute_focus(timestamps)
    assert f.work_block_count == 2


# ── 9. Streak calculation ─────────────────────────────────────────────────────
# 3 messages with small gaps (< 5 min), then 10-min gap, then 2 more.
# The first 3 form a streak; the 10-min gap breaks the streak.
# Streak length = time from first msg to third msg.

def test_streak_calculation():
    now = now_utc()
    # First streak: msgs at -40m, -37m, -34m (gaps = 3 min each, < 5 min)
    # Then 10-min gap (> BREAK_DETECT_SECS=5min) → streak breaks
    # Second streak: -24m, -21m (current, idle 21 min > 5 min)
    # Note: since last msg was 21 min ago (> BREAK_DETECT_SECS), we are on natural break.
    # The streak for the first 3 is: tss[2] - tss[0] = 6 min = 360 s
    timestamps = [
        now - timedelta(minutes=40),
        now - timedelta(minutes=37),
        now - timedelta(minutes=34),
        # 10-min gap
        now - timedelta(minutes=24),
        now - timedelta(minutes=21),   # last msg 21 min ago → natural break
    ]
    f = compute_focus(timestamps)
    # First streak ends at tss[2] (-34m); streak_start was tss[0] (-40m)
    # best_streak at that point = (-34m) - (-40m) = 6 min = 360 s
    # Second streak: starts at tss[3] (-24m), ends at tss[4] (-21m) = 3 min = 180 s
    # (idle > 5 min so cur_streak_end = last_ts = -21m, not now)
    # longest_streak_secs = 360 s
    assert f.longest_streak_secs >= 5 * 60   # first streak >= 6 min
    assert f.longest_streak_secs <= 8 * 60   # not longer than 8 min


# ── 10. Pomodoro counting ─────────────────────────────────────────────────────
# One block > 25 min followed by a break → pomodoros_done=1

def test_pomodoro_counting():
    now = now_utc()
    # Block 1: 75 min ago to 40 min ago = 35 min (≥ 25 min → completed pomodoro)
    # 35-min gap (> BLOCK_GAP_SECS=30min) → new block
    # Block 2: 5 min ago, currently working (not completed yet)
    timestamps = [
        now - timedelta(minutes=75),
        now - timedelta(minutes=65),
        now - timedelta(minutes=55),
        now - timedelta(minutes=45),
        now - timedelta(minutes=40),   # block 1 ends here (40 min ago)
        # 35-min gap
        now - timedelta(minutes=5),    # block 2, recent activity
    ]
    f = compute_focus(timestamps)
    # blocks[0] = (75m ago, 40m ago) = 35 min duration ≥ BREAK_SUGGEST_SECS → pomodoro
    # blocks[1] = current block, last msg 5 min ago (< BREAK_DETECT_SECS=5min? exactly 5)
    # Actually 5 min = BREAK_DETECT_SECS, so idle_secs = ~300 = BREAK_DETECT_SECS → on_natural_break
    # but that makes blocks[-1] a completed potential pomodoro check — duration = 0 min
    # so pomodoros_done = 1 (only from block 0)
    assert f.pomodoros_done >= 1


# ── 11. Sleep gap exclusion ───────────────────────────────────────────────────
# A gap of 9 hours should NOT be counted as longest_break_secs

def test_sleep_gap_excluded():
    now = now_utc()
    # Work yesterday + 9-hour sleep + work today
    timestamps = [
        now - timedelta(hours=11),
        now - timedelta(hours=10),
        # 9-hour gap (≥ SLEEP_SECS=8h) — should be excluded
        now - timedelta(hours=1),
        now - timedelta(minutes=30),
        now - timedelta(minutes=5),
    ]
    f = compute_focus(timestamps)
    # 9-hour gap >= 8 hours = SLEEP_SECS, should be excluded from longest_break_secs
    SLEEP_SECS = 8 * 60 * 60
    assert f.longest_break_secs < SLEEP_SECS


# ── 12. Mixed timezones ───────────────────────────────────────────────────────
# Some timestamps with tzinfo=None (naive), some with UTC.
# compute_focus normalises naive → UTC, so both should work together.

def test_mixed_timezones():
    now = now_utc()
    # Use a gap < BREAK_DETECT_SECS between the two messages so streak stays intact
    # and last message is very recent (< BREAK_DETECT_SECS from now)
    naive_ts = (now - timedelta(minutes=3)).replace(tzinfo=None)   # 3 min ago, naive
    aware_ts = now - timedelta(seconds=30)                          # 30s ago, UTC-aware

    f = compute_focus([naive_ts, aware_ts])
    assert f.is_working is True
    # streak from naive_ts to now: ~3 min (< 5 min)
    assert f.block_seconds >= 2 * 60
    assert f.block_seconds <= 5 * 60
