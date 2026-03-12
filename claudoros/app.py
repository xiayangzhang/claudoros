"""Claudoros — Claude Code session monitor."""
from __future__ import annotations

import subprocess
from datetime import date, datetime, timezone

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.widgets import Static

from .parser import (
    SessionData,
    StatsCache,
    _tz,
    fmt_ago,
    fmt_duration,
    fmt_tokens,
    home_relative,
    load_stats_cache,
    parse_all_sessions,
    truncate,
)
from .pomodoro import (
    BREAK_DETECT_SECS,
    BREAK_SUGGEST_SECS,
    REST_ALARM_SECS,
    FocusStatus,
    compute_focus,
)

POLL_SECS = 2.0
TICK_SECS = 1.0
WORK_ALARM_SECS = 10 * 60   # Claude waiting > 10 min

# ── Colour palettes ───────────────────────────────────────────────────────────

_DARK = {
    "text":    "#cdd6f4",
    "muted":   "#585b70",
    "dim":     "#45475a",
    "name":    "#cdd6f4",
    "path":    "#6c7086",
    "branch":  "#cba6f7",
    "green":   "#a6e3a1",
    "blue":    "#89b4fa",
    "yellow":  "#f9e2af",
    "red":     "#f38ba8",
    "peach":   "#fab387",
    "teal":    "#94e2d5",
    "header":  "#b4befe",
    "border":  "#313244",
    "idle":    "#45475a",
}

_LIGHT = {
    "text":    "#4c4f69",
    "muted":   "#9ca0b0",
    "dim":     "#bcc0cc",
    "name":    "#4c4f69",
    "path":    "#8c8fa1",
    "branch":  "#8839ef",
    "green":   "#40a02b",
    "blue":    "#1e66f5",
    "yellow":  "#df8e1d",
    "red":     "#d20f39",
    "peach":   "#fe640b",
    "teal":    "#179299",
    "header":  "#7287fd",
    "border":  "#ccd0da",
    "idle":    "#dce0e8",
}


def _hl(dark: bool) -> dict:
    return _DARK if dark else _LIGHT


_COLOR_MAP = {"peach": "peach", "yellow": "yellow", "red": "red", "overlay0": "muted"}


def _detect_system_dark() -> bool:
    """Detect macOS dark mode; fallback to True (dark) on error or non-macOS."""
    import sys
    if sys.platform != "darwin":
        return True  # assume dark on non-macOS
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=1,
        )
        return result.stdout.strip().lower() == "dark"
    except Exception:
        return True


def _fmt_timer(total_secs: int) -> str:
    """Live countdown/up display.  < 1 h → MM:SS  |  ≥ 1 h → Xh Ym"""
    m, s = divmod(total_secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{m:02d}:{s:02d}"


def _bar(ratio: float, width: int, color: str, dark: bool, blink_off: bool = False) -> str:
    c = _hl(dark)
    if blink_off:
        return f"[{c['dim']}]{'░' * width}[/]"
    filled = round(max(0.0, min(1.0, ratio)) * width)
    empty  = width - filled
    return f"[{c[color]}]{'█' * filled}[/][{c['dim']}]{'░' * empty}[/]"


# ── Session card ──────────────────────────────────────────────────────────────

def _has_pending_reply(s: SessionData) -> bool:
    """True if Claude replied last — user still owes a response."""
    u_ts = s.last_user_text_ts
    a_ts = s.last_assistant_text_ts
    if not u_ts or not a_ts:
        return False
    return _tz(a_ts) > _tz(u_ts)


def _session_card(s: SessionData, dark: bool, width: int = 60) -> str:
    c = _hl(dark)
    msg_w = width - 5

    # status icon
    if s.is_live:
        icon = f"[{c['green']}]●[/]"
    elif s.is_recent:
        icon = f"[{c['muted']}]○[/]"
    else:
        icon = f"[{c['dim']}]·[/]"

    # ── line 1: icon  name  path  branch ─────────────────────────────────────
    name_part = f"[bold {c['name']}]{s.project_name}[/]"
    path_part = f"[{c['path']}]{home_relative(s.project_path)}[/]"

    branch_part = ""
    if s.git_branch and s.git_branch not in ("HEAD", "main", "master"):
        branch_part = f"  [{c['branch']}]{s.git_branch}[/]"

    line1 = f"  {icon}  {name_part}   {path_part}{branch_part}"

    # ── line 2: last message ──────────────────────────────────────────────────
    u_ts = s.last_user_text_ts
    a_ts = s.last_assistant_text_ts
    u_tz = _tz(u_ts) if u_ts else None
    a_tz = _tz(a_ts) if a_ts else None

    last_text = last_who = None
    if u_tz and a_tz:
        if u_tz >= a_tz:
            last_text, last_who = s.last_user_text, "you"
        else:
            last_text, last_who = s.last_assistant_text, "claude"
    elif u_tz:
        last_text, last_who = s.last_user_text, "you"
    elif a_tz:
        last_text, last_who = s.last_assistant_text, "claude"

    line2 = ""
    if last_text:
        preview = truncate(last_text, msg_w)
        who_color = c["blue"] if last_who == "claude" else c["teal"]
        line2 = f"     [{who_color}]{last_who}[/]  [{c['text']}]{preview}[/]"

    # ── line 3: status + stats ────────────────────────────────────────────────
    badges: list[str] = []

    if s.is_live:
        st = s.status
        if st == "thinking":
            badges.append(f"[{c['muted']}]thinking…[/]")
        elif st == "waiting":
            badges.append(f"[bold {c['green']}]● waiting for you[/]")

    stats_parts: list[str] = []
    if s.user_message_count:
        stats_parts.append(f"{s.user_message_count} msgs")
    if s.output_tokens:
        stats_parts.append(f"{fmt_tokens(s.output_tokens)} out")
    secs = s.seconds_since_activity
    if secs < 86400:
        stats_parts.append(fmt_ago(secs))
    elif s.duration_seconds:
        stats_parts.append(fmt_duration(s.duration_seconds))

    stats_str = f"[{c['muted']}]{('  ·  ').join(stats_parts)}[/]" if stats_parts else ""

    if badges and stats_str:
        line3 = f"     {badges[0]}   {stats_str}"
    elif badges:
        line3 = f"     {badges[0]}"
    elif stats_str:
        line3 = f"     {stats_str}"
    else:
        line3 = ""

    parts = [line1]
    if line2:
        parts.append(line2)
    if line3:
        parts.append(line3)

    return "\n".join(parts)


# ── Focus context message ─────────────────────────────────────────────────────

def _focus_context_message(
    focus: FocusStatus,
    sessions: list[SessionData],
    c: dict,
    block_msgs: int,
) -> str:
    if not (focus.suggest_break or focus.urgent_break):
        return ""

    block_mins = max(1, focus.block_seconds // 60)
    col = c["red"] if focus.urgent_break else c["yellow"]

    all_secs  = sum(s.total_response_secs for s in sessions)
    all_count = sum(s.response_count for s in sessions)
    avg_wait  = all_secs / all_count if all_count else None

    live_msgs = sum(s.user_message_count for s in sessions if s.is_live)
    seed = int(focus.block_start.timestamp()) if focus.block_start else 0

    def muted(text: str) -> str:
        return f"  [{c['muted']}]{text}[/]"

    def hi(text: str) -> str:
        return f"  [{col}]{text}[/]"

    # ── very low message count for a long block ───────────────────────────────
    if live_msgs <= 3 and focus.block_seconds >= 25 * 60:
        msgs_per_min = live_msgs / max(1, block_mins)
        jokes = [
            lambda: (hi(f"{block_mins} min, {live_msgs} messages"), muted("that's one every " + fmt_duration(block_mins * 60 // max(1, live_msgs)))),
            lambda: (muted("at this pace, claude thinks you left"),),
            lambda: (hi(f"{live_msgs} msgs in {block_mins} min"),   muted("is this a work session or a screensaver?")),
        ]
        lines = jokes[seed % len(jokes)]()
        return "\n".join(lines)

    # ── high avg wait time ────────────────────────────────────────────────────
    if avg_wait is not None and avg_wait > 300:
        wait_str = fmt_duration(int(avg_wait)) if avg_wait >= 60 else f"{int(avg_wait)}s"
        jokes = [
            lambda: (hi(f"avg {wait_str} to reply to claude"), muted("are you making tea or just vibing?")),
            lambda: (muted("at this rate, the AI is waiting for you,"), muted("not the other way around")),
            lambda: (muted(f'claude: "…{wait_str} and still no reply?"'),),
        ]
        lines = jokes[seed % len(jokes)]()
        return "\n".join(lines)

    # ── overtime — been going a while past 25 min ─────────────────────────────
    overtime_mins = max(0, (focus.block_seconds - 25 * 60) // 60)
    if overtime_mins >= 5:
        longest_m = focus.longest_streak_secs // 60
        jokes = [
            lambda: (hi(f"{overtime_mins} min past the pomodoro mark"), muted("your body is not a cron job")),
            lambda: (hi(f"{block_mins} min straight"), muted(f"longest streak today: {longest_m} min — is this it?")),
            lambda: (muted(f"{block_msgs} messages, {block_mins} min,"), hi("still no break?")),
        ]
        lines = jokes[seed % len(jokes)]()
        return "\n".join(lines)

    # ── default: clean summary ────────────────────────────────────────────────
    return (
        f"  [{col}]{block_msgs} messages  {block_mins} min[/]\n"
        f"  [{c['muted']}]time for a break[/]"
    )


# ── Claude bar ───────────────────────────────────────────────────────────────

def _claude_bar_info(sessions: list[SessionData]) -> tuple[str, int] | None:
    """
    Returns (mode, seconds) for the Claude status bar, or None if nothing relevant.
    mode = "thinking" | "waiting"

    "thinking": user sent the last message → Claude is working.
                Uses recent (1 hr) threshold because Claude can run for a long time
                with no JSONL output yet, causing is_live to drop to False.
    "waiting":  Claude replied last → waiting for the user.
                Only shown for live sessions (< 5 min) so we don't spam stale waits.
    """
    now = datetime.now(timezone.utc)
    thinking_secs: list[int] = []
    waiting_secs:  list[int] = []

    for s in sessions:
        if not s.is_recent:   # older than 1 hr — skip entirely
            continue
        u_ts = s.last_user_text_ts
        a_ts = s.last_assistant_text_ts
        if u_ts is None:
            continue
        u_tz = _tz(u_ts)
        a_tz = _tz(a_ts) if a_ts else None

        if a_tz is None or u_tz > a_tz:
            # User messaged last → Claude is (potentially) thinking
            secs = int((now - u_tz).total_seconds())
            thinking_secs.append(secs)
        elif a_tz > u_tz:
            # Claude replied last → waiting for user (any recent session)
            secs = int((now - a_tz).total_seconds())
            waiting_secs.append(secs)

    if thinking_secs:
        return ("thinking", max(thinking_secs))
    if waiting_secs:
        return ("waiting", max(waiting_secs))

    # No active/waiting sessions — find time since Claude last responded
    last_a: datetime | None = None
    for s in sessions:
        if s.last_assistant_text_ts:
            ts = _tz(s.last_assistant_text_ts)
            if last_a is None or ts > last_a:
                last_a = ts
    if last_a:
        resting_secs = int((now - last_a).total_seconds())
        return ("resting", resting_secs)
    return None


# ── Claude-side stat helpers ──────────────────────────────────────────────────

def _max_concurrent(today_sessions: list[SessionData]) -> int:
    """Peak number of overlapping sessions by time range."""
    events: list[tuple[datetime, int]] = []
    for s in today_sessions:
        if s.start_time and s.last_activity:
            events.append((_tz(s.start_time), +1))
            events.append((_tz(s.last_activity), -1))
    if not events:
        return 0
    events.sort(key=lambda x: (x[0], x[1]))
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def _idle_today_secs(today_sessions: list[SessionData]) -> int:
    """Total seconds with no active sessions today."""
    ranges: list[list[datetime]] = []
    for s in today_sessions:
        if s.start_time and s.last_activity:
            ranges.append([_tz(s.start_time), _tz(s.last_activity)])
    if not ranges:
        return 0
    ranges.sort(key=lambda r: r[0])
    merged: list[list[datetime]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    total = 0
    for i in range(1, len(merged)):
        gap = (merged[i][0] - merged[i - 1][1]).total_seconds()
        if gap > 0:
            total += int(gap)
    return total


def _longest_wait_session(today_sessions: list[SessionData]) -> tuple[SessionData | None, float]:
    best_s: SessionData | None = None
    best_secs = 0.0
    for s in today_sessions:
        if s.max_response_secs > best_secs:
            best_secs = s.max_response_secs
            best_s = s
    return best_s, best_secs


# ── Panels ────────────────────────────────────────────────────────────────────

class SessionsPanel(ScrollableContainer):
    DEFAULT_CSS = """
    SessionsPanel {
        width: 1fr;
        height: 1fr;
        border: none;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="sessions-body")

    def refresh_sessions(self, sessions: list[SessionData]) -> None:
        body = self.query_one("#sessions-body", Static)
        body.update(self._build(sessions))

    def _is_dark(self) -> bool:
        return self.app._dark_mode

    def _build(self, sessions: list[SessionData]) -> str:
        dark = self._is_dark()
        c = _hl(dark)
        today_date = datetime.now(timezone.utc).date()

        live    = [s for s in sessions if s.is_live]
        recent  = [s for s in sessions if s.is_recent and not s.is_live]
        earlier = [s for s in sessions if _is_today(s, today_date) and not s.is_recent]

        parts: list[str] = [_timeline_heatmap(sessions, dark), ""]

        if live:
            parts.append(f"  [{c['header']}]live[/]  [{c['green']}]{len(live)}[/]")
            parts.append("")
            for s in live:
                parts.append(_session_card(s, dark))
                parts.append("")

        if recent:
            parts.append(f"  [{c['header']}]recent[/]  [{c['muted']}]{len(recent)}[/]")
            parts.append("")
            for s in recent:
                icon = f"[{c['muted']}]○[/]"
                name = f"[bold {c['name']}]{s.project_name}[/]"
                ago  = f"[{c['muted']}]{fmt_ago(s.seconds_since_activity)}[/]"
                # last message preview
                u_ts = _tz(s.last_user_text_ts)  if s.last_user_text_ts  else None
                a_ts = _tz(s.last_assistant_text_ts) if s.last_assistant_text_ts else None
                preview = ""
                if u_ts and a_ts:
                    last_text, last_who = (s.last_user_text, "you") if u_ts >= a_ts else (s.last_assistant_text, "claude")
                elif u_ts:
                    last_text, last_who = s.last_user_text, "you"
                elif a_ts:
                    last_text, last_who = s.last_assistant_text, "claude"
                else:
                    last_text = last_who = None
                if last_text:
                    who_col = c["blue"] if last_who == "claude" else c["teal"]
                    preview = f"  [{who_col}]{last_who}[/]  [{c['muted']}]{truncate(last_text, 28)}[/]"
                waiting = ""
                if _has_pending_reply(s):
                    waiting = f"  [{c['yellow']}]●[/]"
                parts.append(f"  {icon}  {name}  {ago}{preview}{waiting}")
            parts.append("")

        if earlier:
            parts.append(f"  [{c['header']}]earlier today[/]  [{c['dim']}]{len(earlier)}[/]")
            parts.append("")
            for s in earlier:
                ago = fmt_ago(s.seconds_since_activity)
                name = f"[bold {c['name']}]{s.project_name}[/]"
                parts.append(f"  [{c['dim']}]·[/]  {name}  [{c['dim']}]{ago}[/]")
                # last message preview
                u_ts = s.last_user_text_ts
                a_ts = s.last_assistant_text_ts
                u_tz = _tz(u_ts) if u_ts else None
                a_tz = _tz(a_ts) if a_ts else None
                if u_tz and a_tz:
                    last_text, last_who = (s.last_user_text, "you") if u_tz >= a_tz else (s.last_assistant_text, "claude")
                elif u_tz:
                    last_text, last_who = s.last_user_text, "you"
                elif a_tz:
                    last_text, last_who = s.last_assistant_text, "claude"
                else:
                    last_text = last_who = None
                if last_text:
                    who_col = c["blue"] if last_who == "claude" else c["teal"]
                    preview = truncate(last_text, 38)
                    parts.append(f"     [{who_col}]{last_who}[/]  [{c['dim']}]{preview}[/]")
            parts.append("")

        if not live and not recent and not earlier:
            parts.append(f"  [{c['muted']}]no sessions today[/]")
            parts.append("")
            parts.append(f"  [{c['dim']}]sessions appear here when claude code is running[/]")

        return "\n".join(parts)


_BREAK_SARCASM = [
    "coming back anytime soon?",
    "claude misses you",
    "your terminal is getting lonely",
]


class SidePanel(Container):
    DEFAULT_CSS = """
    SidePanel {
        width: 32;
        height: 1fr;
        border: none;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="side-body")

    def _is_dark(self) -> bool:
        return self.app._dark_mode

    def refresh_data(
        self,
        focus: FocusStatus,
        sessions: list[SessionData],
        rest_muted: bool,
        work_muted: bool,
        blink_off: bool = False,
    ) -> None:
        dark = self._is_dark()
        c = _hl(dark)

        block_msgs = 0
        if focus.block_start:
            bs = _tz(focus.block_start)
            for s in sessions:
                st = s.start_time
                if st and _tz(st) >= bs:
                    block_msgs += s.user_message_count

        claude_bar = _claude_bar_info(sessions)
        self.query_one("#side-body", Static).update(
            self._build_side(focus, sessions, rest_muted, work_muted, c, dark, block_msgs, blink_off, claude_bar)
        )

    def _build_side(
        self,
        focus: FocusStatus,
        sessions: list[SessionData],
        rest_muted: bool,
        work_muted: bool,
        c: dict,
        dark: bool,
        block_msgs: int = 0,
        blink_off: bool = False,
        claude_bar: tuple[str, int] | None = None,
    ) -> str:
        lines: list[str] = []
        today_date = datetime.now(timezone.utc).date()
        today_sessions = [s for s in sessions if _is_today(s, today_date)]

        def stat(label: str, value: str) -> str:
            return f"  [{c['muted']}]{label}[/]  [{c['text']}]{value}[/]"

        def sep() -> None:
            lines.append(f"  [{c['dim']}]{'─' * 20}[/]")

        # ══ claude section ════════════════════════════════════════════════════
        lines.append(f"  [{c['header']}]claude[/]")
        lines.append("")

        if claude_bar is None:
            lines.append("  " + _bar(0, 22, "muted", dark))
            lines.append(f"  [{c['muted']}]quiet[/]")
        else:
            mode, secs = claude_bar
            if mode == "thinking":
                # Fills toward 5 min — past that Claude's clearly deep in it
                ratio = min(1.0, secs / BREAK_DETECT_SECS)
                lines.append("  " + _bar(ratio, 22, "blue", dark))
                t = _fmt_timer(secs)
                if secs < 30:
                    label = "starting up…"
                elif secs < BREAK_DETECT_SECS:
                    label = "thinking"
                elif secs < 10 * 60:
                    label = "deep in thought"
                else:
                    label = "really deep in thought"
                lines.append(f"  [{c['blue']}]{t}[/]  {label}")

            elif mode == "waiting":
                ratio = min(1.0, secs / WORK_ALARM_SECS)
                if secs < 60:
                    col, label = "muted", "just replied"
                elif secs < WORK_ALARM_SECS // 2:
                    col, label = "muted", "waiting for you"
                elif secs < WORK_ALARM_SECS:
                    col, label = "yellow", "still waiting…"
                else:
                    col, label = "yellow", "getting impatient"
                lines.append("  " + _bar(ratio, 22, col, dark))
                lines.append(f"  [{c[col]}]{_fmt_timer(secs)}[/]  {label}")

            else:  # resting
                # Soft dim bar that slowly fills — just for visual interest
                REST_SHOW_SECS = 30 * 60  # fills over 30 min
                ratio = min(1.0, secs / REST_SHOW_SECS)
                lines.append("  " + _bar(ratio, 22, "dim", dark))
                t = _fmt_timer(secs)
                if secs < 5 * 60:
                    label = "taking a breather"
                elif secs < 15 * 60:
                    label = "on a break"
                elif secs < 30 * 60:
                    label = "enjoying the silence"
                elif secs < 60 * 60:
                    label = "probably napping"
                else:
                    label = "deeply offline"
                lines.append(f"  [{c['dim']}]{t}[/]  {label}")

        lines.append("")
        lines.append(stat("sessions", str(len(today_sessions))))

        max_conc = _max_concurrent(today_sessions)
        if max_conc > 1:
            lines.append(stat("max concurrent", str(max_conc)))

        idle_secs = _idle_today_secs(today_sessions)
        if idle_secs >= 60:
            lines.append(stat("idle today", fmt_duration(idle_secs)))

        # Claude reply stats (how long user takes to reply to Claude)
        all_reply_secs  = sum(s.total_response_secs for s in today_sessions)
        all_reply_count = sum(s.response_count for s in today_sessions)
        max_reply_secs  = max((s.max_response_secs for s in today_sessions
                               if s.max_response_secs > 0), default=0.0)
        if all_reply_count:
            avg_r = all_reply_secs / all_reply_count
            lines.append(stat("avg wait", fmt_duration(int(avg_r)) if avg_r >= 60 else f"{int(avg_r)}s"))
        if max_reply_secs >= 60:
            lines.append(stat("max wait", fmt_duration(int(max_reply_secs))))

        lw_sess, lw_secs = _longest_wait_session(today_sessions)
        if lw_sess and lw_secs >= 60:
            lines.append(stat("longest wait", fmt_duration(int(lw_secs))))
            lines.append(f"  [{c['dim']}]  ↳ {truncate(lw_sess.project_name, 10)}[/]")

        # ══ you section ═══════════════════════════════════════════════════════
        lines.append("")
        sep()
        lines.append(f"  [{c['header']}]you[/]")
        lines.append("")

        if focus.on_natural_break:
            ratio = min(1.0, focus.natural_break_secs / BREAK_DETECT_SECS)
            lines.append("  " + _bar(ratio, 22, "teal", dark))
            lines.append(f"  [{c['teal']}]{_fmt_timer(focus.natural_break_secs)}[/]  on break")
            if focus.natural_break_secs > 30 * 60:
                seed = int(focus.last_msg_ts.timestamp()) if focus.last_msg_ts else 0
                lines.append(f"  [{c['muted']}]{_BREAK_SARCASM[seed % len(_BREAK_SARCASM)]}[/]")
            elif focus.natural_break_secs > 15 * 60:
                lines.append(f"  [{c['muted']}]that's quite the break…[/]")

        elif focus.is_working:
            bar_color = _COLOR_MAP.get(focus.status_color, "muted")
            should_blink = blink_off and (focus.suggest_break or focus.urgent_break)
            lines.append("  " + _bar(focus.progress, 22, bar_color, dark, blink_off=should_blink))
            overtime = focus.overtime_seconds
            if overtime:
                lines.append(
                    f"  [{c[bar_color]}]{_fmt_timer(focus.block_seconds)}[/]"
                    f"  [{c['muted']}]+{fmt_duration(overtime)} over[/]"
                )
            else:
                lines.append(f"  [{c[bar_color]}]{_fmt_timer(focus.block_seconds)}[/]  focused")
            if focus.block_start:
                local = focus.block_start.astimezone().strftime("%H:%M")
                lines.append(f"  [{c['muted']}]since {local}[/]")
            if focus.suggest_break or focus.urgent_break:
                ctx = _focus_context_message(focus, sessions, c, block_msgs)
                if ctx:
                    lines.append("")
                    lines.extend(ctx.split("\n"))

        else:
            lines.append("  " + _bar(0, 22, "muted", dark))
            lines.append(f"  [{c['muted']}]idle[/]")
            if focus.idle_seconds and focus.last_msg_ts:
                lines.append(f"  [{c['muted']}]{fmt_ago(focus.idle_seconds)}[/]")

        lines.append("")
        lines.append(stat("pomodoros", str(focus.pomodoros_done)))
        lines.append(stat("work blocks", str(focus.work_block_count)))

        if focus.longest_streak_secs > 0:
            lines.append(stat("longest work", fmt_duration(focus.longest_streak_secs)))

        if focus.longest_break_secs >= 60:
            lines.append(stat("longest break", fmt_duration(focus.longest_break_secs)))

        all_reply_secs  = sum(s.total_response_secs for s in today_sessions)
        all_reply_count = sum(s.response_count for s in today_sessions)
        if all_reply_count:
            avg_r = all_reply_secs / all_reply_count
            val = fmt_duration(int(avg_r)) if avg_r >= 60 else f"{int(avg_r)}s"
            lines.append(stat("avg reply", val))

        lines.append("")

        return "\n".join(lines)


# ── Top bar ───────────────────────────────────────────────────────────────────

def _is_today(s: SessionData, today_date) -> bool:
    # Use last_activity first — a long-running session that started yesterday UTC
    # but is active today should still count as today's session.
    ref = s.last_activity or s.start_time
    if not ref:
        return False
    ref = _tz(ref)
    return ref.astimezone().date() == today_date



def _footer(dark: bool, rest_muted: bool, work_muted: bool) -> str:
    c = _hl(dark)
    dim, txt, muted = c["dim"], c["text"], c["muted"]

    def key(k: str, label: str, state: str | None = None) -> str:
        state_part = f" [{muted}]{state}[/]" if state else ""
        return f"[{dim}]{k}[/] [{txt}]{label}[/]{state_part}"

    rest_state = "off" if rest_muted else "on"
    work_state = "off" if work_muted else "on"

    parts = [
        key("q", "quit"),
        key("s", "rest alarm", rest_state),
        key("w", "work alarm", work_state),
    ]
    return "  " + f"  [{dim}]·[/]  ".join(parts)


def _topbar(
    sessions: list[SessionData],
    focus: FocusStatus,
    dark: bool,
    rest_muted: bool = True,
    work_muted: bool = True,
) -> str:
    c = _hl(dark)
    live     = [s for s in sessions if s.is_live]
    thinking = [s for s in live if s.status == "thinking"]
    waiting  = [s for s in live if s.status == "waiting"]

    parts = [f"[bold {c['header']}]claudoros[/]", f"[{c['dim']}]│[/]"]

    if live:
        parts.append(f"[{c['green']}]● {len(live)} live[/]")
    else:
        parts.append(f"[{c['muted']}]idle[/]")

    if thinking:
        parts.append(f"[{c['muted']}]{len(thinking)} thinking[/]")
    if waiting:
        parts.append(f"[bold {c['green']}]● {len(waiting)} reply[/]")

    parts.append(f"[{c['dim']}]│[/]")

    today_date = datetime.now(timezone.utc).date()
    today = [s for s in sessions if _is_today(s, today_date)]
    u_msgs = sum(s.user_message_count    for s in today)
    a_msgs = sum(s.assistant_message_count for s in today)
    breakdown = f" [{c['dim']}]({u_msgs} you · {a_msgs} cld)[/]" if (u_msgs or a_msgs) else ""
    parts.append(f"[{c['muted']}]{u_msgs + a_msgs} msgs today[/]{breakdown}")

    parts.append(f"[{c['dim']}]│[/]")

    if focus.on_natural_break:
        parts.append(f"[{c['teal']}]{_fmt_timer(focus.natural_break_secs)} break[/]")
    elif focus.is_working:
        col_key = _COLOR_MAP.get(focus.status_color, "muted")
        parts.append(f"[{c[col_key]}]{_fmt_timer(focus.block_seconds)} focused[/]")
        if focus.suggest_break and not focus.urgent_break:
            parts.append(f"[{c['yellow']}]break?[/]")
        elif focus.urgent_break:
            parts.append(f"[{c['red']}]break![/]")
    else:
        parts.append(f"[{c['muted']}]idle[/]")

    # alarms (compact: show only when on)
    parts.append(f"[{c['dim']}]│[/]")
    alarm_parts = []
    if not rest_muted:
        alarm_parts.append(f"[{c['green']}]⏰ rest[/]")
    if not work_muted:
        alarm_parts.append(f"[{c['green']}]⏰ work[/]")
    if alarm_parts:
        parts.extend(alarm_parts)
    else:
        parts.append(f"[{c['dim']}]alarms off[/]")

    return f"  {'  '.join(parts)}"


# ── Banner ────────────────────────────────────────────────────────────────────

def _banner(
    sessions: list[SessionData],
    dark: bool,
    focus: FocusStatus | None = None,
) -> str:
    c = _hl(dark)
    today_date = datetime.now(timezone.utc).date()
    dim, txt, hi = c["muted"], c["text"], c["peach"]

    today_sessions = [s for s in sessions if _is_today(s, today_date)]
    today_out  = sum(s.output_tokens for s in today_sessions)
    today_msgs = sum(s.user_message_count for s in today_sessions)

    if today_out == 0 and today_msgs == 0:
        return f"  [{dim}]no activity today yet[/]"

    # reply stats
    reply_secs  = sum(s.total_response_secs for s in today_sessions)
    reply_count = sum(s.response_count for s in today_sessions)
    avg_reply   = reply_secs / reply_count if reply_count else None

    # focus stats
    pomodoros    = focus.pomodoros_done if focus else 0
    longest_m    = (focus.longest_streak_secs // 60) if focus else 0
    longest_b_m  = (focus.longest_break_secs  // 60) if focus else 0

    # token-derived
    words       = today_out * 0.75
    pages       = today_out / 400
    typing_hrs  = words / 60 / 60
    reading_hrs = words / 250 / 60
    lotr_tokens = 670_000

    ts_sessions = [s for s in today_sessions if s.start_time]
    if ts_sessions:
        earliest  = min(_tz(s.start_time) for s in ts_sessions)
        work_mins = max(1, (datetime.now(timezone.utc) - earliest).total_seconds() / 60)
    else:
        work_mins = max(1, datetime.now().hour * 60 + datetime.now().minute)
    wpm = words / work_mins if words else 0

    # Build pool of available quips (only add if data is meaningful)
    variants: list[str] = []

    if today_out > 0:
        lotr = today_out / lotr_tokens
        variants.append(
            f"  [{dim}]claude wrote you[/] [{hi}]{pages:.0f} pages[/]"
            f" [{dim}]today —[/] [{hi}]{lotr:.2f}×[/] [{dim}]the LotR trilogy[/]"
        )
        variants.append(
            f"  [{dim}]a human typist at 60 wpm would need[/] [{hi}]{typing_hrs:.1f}h[/]"
            f" [{dim}]to type what claude generated today[/]"
        )
        variants.append(
            f"  [{dim}]claude wrote you[/] [{hi}]{reading_hrs:.1f}h[/]"
            f" [{dim}]of reading material across[/] [{txt}]{today_msgs}[/] [{dim}]msgs[/]"
        )
        if wpm > 0:
            variants.append(
                f"  [{dim}]since your first session, claude has been generating[/]"
                f" [{hi}]{wpm:.0f} wpm[/] [{dim}]for you[/]"
            )

    if avg_reply is not None and reply_count >= 3:
        avg_str = fmt_duration(int(avg_reply)) if avg_reply >= 60 else f"{avg_reply:.0f}s"
        variants.append(
            f"  [{dim}]you've replied to claude[/] [{hi}]{reply_count}[/]"
            f" [{dim}]times today, avg[/] [{hi}]{avg_str}[/] [{dim}]each[/]"
        )

    if pomodoros >= 1:
        focused_min = pomodoros * 25
        variants.append(
            f"  [{hi}]{pomodoros}[/] [{dim}]pomodoro{'s' if pomodoros>1 else ''} done[/]"
            f" [{dim}]—[/] [{hi}]{focused_min}[/] [{dim}]min of focused work today[/]"
        )

    if longest_m >= 10:
        variants.append(
            f"  [{dim}]longest unbroken stretch today:[/] [{hi}]{longest_m} min[/]"
            + (f"  [{dim}]longest break: {longest_b_m} min[/]" if longest_b_m >= 5 else "")
        )

    if today_msgs >= 5 and reply_count >= 1:
        ratio = reply_secs / max(1, today_out)  # how much time per token
        if ratio > 0.05:
            variants.append(
                f"  [{dim}]you sent[/] [{hi}]{today_msgs}[/] [{dim}]messages today[/]"
                f" [{dim}]— claude is earning its compute[/]"
            )

    if not variants:
        return f"  [{dim}]session in progress[/]"

    idx = datetime.now().minute % len(variants)
    return variants[idx]


# ── Timeline shared helpers ───────────────────────────────────────────────────

_TIMELINE_SLOTS = 24   # hourly buckets
_SLOT_W         = 2    # display chars per slot  →  24 × 2 = 48 chars wide


def _bucket_today(sessions: list[SessionData], attr: str) -> list[int]:
    local_today = datetime.now().date()
    counts = [0] * _TIMELINE_SLOTS
    for s in sessions:
        for ts in getattr(s, attr):
            lt = _tz(ts).astimezone()
            if lt.date() == local_today:
                counts[lt.hour] += 1
    return counts


def _slot_levels(counts: list[int], n: int) -> list[int]:
    mx = max(counts) or 1
    return [0 if c == 0 else max(1, (c * n + mx - 1) // mx) for c in counts]


def _timeline_axis() -> str:
    """48-char axis with hour labels; · marks current hour."""
    W = _TIMELINE_SLOTS * _SLOT_W
    axis = [" "] * W
    for hour, label in [(0, "0"), (6, "6"), (12, "12"), (18, "18")]:
        pos = hour * _SLOT_W
        for k, ch in enumerate(label):
            if pos + k < W:
                axis[pos + k] = ch
    now_pos = datetime.now().hour * _SLOT_W
    if 0 <= now_pos < W and axis[now_pos] == " ":
        axis[now_pos] = "·"
    return "".join(axis) + " 24"


def _colorize_run(text: str, filled_chars: set, fill_col: str, dim_col: str) -> str:
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        col = fill_col if ch in filled_chars else dim_col
        j = i + 1
        while j < len(text) and text[j] == ch:
            j += 1
        out.append(f"[{col}]{text[i:j]}[/]")
        i = j
    return "".join(out)


# ── Heatmap timeline ──────────────────────────────────────────────────────────

def _timeline_heatmap(sessions: list[SessionData], dark: bool) -> str:
    """3 rows: axis + you + cld, block chars encode density."""
    c   = _hl(dark)
    N   = 4
    CHS = ["░", "▒", "▓", "█"]   # density chars, low → high

    ul = _slot_levels(_bucket_today(sessions, "user_msg_timestamps"),  N)
    cl = _slot_levels(_bucket_today(sessions, "assistant_msg_timestamps"), N)

    def render(levels: list[int], fill_col: str) -> str:
        flat = "".join(CHS[min(lv, N - 1)] * _SLOT_W for lv in levels)
        return _colorize_run(flat, {"▒", "▓", "█"}, fill_col, c["dim"])

    LW, pad = 6, "  "
    def lbl(text: str, col: str) -> str:
        return f"[{col}]{text}[/]" + " " * (LW - len(text))

    return "\n".join([
        f"{pad}{' ' * LW}[{c['dim']}]{_timeline_axis()}[/]",
        f"{pad}{lbl('you', c['teal'])}{render(ul, c['teal'])}",
        f"{pad}{lbl('cld', c['blue'])}{render(cl, c['blue'])}",
    ])




# ── App ───────────────────────────────────────────────────────────────────────

class ClaudoroApp(App):
    CSS = """
    Screen {
        layers: base;
        background: ansi_default;
    }
    Horizontal, Vertical, ScrollableContainer, Container, Static {
        background: ansi_default;
    }
    #topbar {
        height: 1;
        dock: top;
        padding: 0 0;
    }
    #main {
        height: 1fr;
        margin-top: 1;
    }
    SessionsPanel {
        height: 1fr;
        border: round $panel;
    }
    SidePanel {
        height: 1fr;
        border: round $panel;
    }
    #bottom-bar { height: auto; dock: bottom; }
    #banner     { height: 1; padding: 0 1; }
    #footer-bar { height: 1; padding: 0 1; }
    """

    BINDINGS = [
        Binding("q", "quit",              "Quit",   show=False),
        Binding("s", "toggle_rest_alarm", "Rest",   show=False),
        Binding("w", "toggle_work_alarm", "Work",   show=False),
        Binding("R", "hard_refresh",      "Refresh",show=False),
    ]

    def __init__(self) -> None:
        super().__init__(ansi_color=True)
        self._dark_mode: bool = _detect_system_dark()
        self._sessions: list[SessionData] = []
        self._stats = StatsCache()
        self._focus = compute_focus([])
        self._blink_state: bool = False
        # Alarm state — both muted by default
        self._rest_alarm_muted: bool = True
        self._work_alarm_muted: bool = True
        # Dedup: avoid replaying same alarm for same block/session
        self._alerted_rest_block: datetime | None = None
        self._alerted_work_sessions: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Horizontal(id="main"):
            yield SessionsPanel(id="sessions-panel")
            yield SidePanel(id="side-panel")
        with Vertical(id="bottom-bar"):
            yield Static("", id="banner")
            yield Static("", id="footer-bar")

    def on_mount(self) -> None:
        self._stats = load_stats_cache()
        self._do_refresh()
        self.set_interval(POLL_SECS, self._do_refresh)
        self.set_interval(TICK_SECS, self._tick)

    # ── actions ───────────────────────────────────────────────────────────────

    def action_toggle_rest_alarm(self) -> None:
        self._rest_alarm_muted = not self._rest_alarm_muted

    def action_toggle_work_alarm(self) -> None:
        self._work_alarm_muted = not self._work_alarm_muted

    def action_hard_refresh(self) -> None:
        self._stats = load_stats_cache()
        self._do_refresh()

    # ── sound ─────────────────────────────────────────────────────────────────

    def _play_sound(self) -> None:
        import sys
        try:
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["afplay", "/System/Library/Sounds/Glass.aiff"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                # Linux: try paplay then aplay
                for cmd in [["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
                            ["aplay", "/usr/share/sounds/alsa/Front_Center.wav"]]:
                    result = subprocess.run(["which", cmd[0]], capture_output=True)
                    if result.returncode == 0:
                        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        break
        except Exception:
            pass

    def _maybe_play_alerts(self, focus: FocusStatus, sessions: list[SessionData]) -> None:
        now = datetime.now(timezone.utc)

        # Rest alarm: working > REST_ALARM_SECS
        if not self._rest_alarm_muted and focus.is_working:
            bs = focus.block_start
            if focus.block_seconds >= REST_ALARM_SECS and bs != self._alerted_rest_block:
                self._alerted_rest_block = bs
                self._play_sound()
                return

        # Work alarm: any live session where Claude replied but user hasn't in > WORK_ALARM_SECS
        if not self._work_alarm_muted:
            for s in sessions:
                if not s.is_live:
                    continue
                a_ts = s.last_assistant_text_ts
                u_ts = s.last_user_text_ts
                if not a_ts:
                    continue
                # Claude replied more recently than user
                if u_ts and _tz(u_ts) >= _tz(a_ts):
                    continue
                wait_secs = (now - _tz(a_ts)).total_seconds()
                if wait_secs >= WORK_ALARM_SECS and s.session_id not in self._alerted_work_sessions:
                    self._alerted_work_sessions.add(s.session_id)
                    self._play_sound()
                    break

    # ── data ──────────────────────────────────────────────────────────────────

    def _collect_user_timestamps(self) -> list[datetime]:
        """Collect all real user message timestamps from today's and recent sessions."""
        today = datetime.now(timezone.utc).date()
        tss: list[datetime] = []
        for s in self._sessions:
            if _is_today(s, today) or s.is_recent:
                tss.extend(s.user_msg_timestamps)
        return tss

    def _compute_focus(self) -> FocusStatus:
        return compute_focus(self._collect_user_timestamps())

    def _do_refresh(self) -> None:
        self._dark_mode = _detect_system_dark()
        self._sessions = parse_all_sessions(since_seconds=86400 * 7)
        self._focus = self._compute_focus()
        # Prune work alarm dedup for sessions no longer live
        live_ids = {s.session_id for s in self._sessions if s.is_live}
        self._alerted_work_sessions &= live_ids
        self._update_ui()

    def _tick(self) -> None:
        self._focus = self._compute_focus()
        self._maybe_play_alerts(self._focus, self._sessions)
        self._blink_state = not self._blink_state
        self._update_ui()

    def _update_ui(self) -> None:
        dark = self._dark_mode

        self.query_one("#topbar", Static).update(
            _topbar(self._sessions, self._focus, dark,
                    self._rest_alarm_muted, self._work_alarm_muted)
        )
        self.query_one("#sessions-panel", SessionsPanel).refresh_sessions(self._sessions)
        self.query_one("#side-panel", SidePanel).refresh_data(
            self._focus,
            self._sessions,
            self._rest_alarm_muted,
            self._work_alarm_muted,
            self._blink_state,
        )
        self.query_one("#banner", Static).update(
            _banner(self._sessions, dark, self._focus)
        )
        self.query_one("#footer-bar", Static).update(
            _footer(dark, self._rest_alarm_muted, self._work_alarm_muted)
        )
