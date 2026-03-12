"""Parse Claude Code JSONL session files."""
from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional


CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
STATS_CACHE = CLAUDE_DIR / "stats-cache.json"

LIVE_THRESHOLD_SECS = 300     # 5 min  → "live"
RECENT_THRESHOLD_SECS = 3600  # 1 hr  → "recent"


# ─── path helpers ─────────────────────────────────────────────────────────────

def decode_project_path(dir_name: str) -> str:
    """'-Volumes-leoyun-foo' → '/Volumes/leoyun/foo'"""
    raw = dir_name.replace("-", "/")
    return raw if raw.startswith("/") else "/" + raw


def project_name_from_path(path: str) -> str:
    return Path(path).name or path.split("/")[-2] or path


def home_relative(path: str) -> str:
    """Shorten path: try ~/... first, else show last 3 components."""
    p = Path(path)
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        parts = p.parts
        if len(parts) > 2:
            return "…/" + "/".join(parts[-2:])
        return path


# ─── data model ───────────────────────────────────────────────────────────────

SessionStatus = Literal["thinking", "waiting", "idle"]


@dataclass
class SessionData:
    session_id: str
    project_dir: str        # hashed dir name
    project_path: str       # decoded real path
    project_name: str       # last path component
    slug: Optional[str] = None
    git_branch: Optional[str] = None
    model: Optional[str] = None
    cwd: Optional[str] = None
    start_time: Optional[datetime] = None
    last_activity: Optional[datetime] = None

    # Message counts
    user_message_count: int = 0      # real human messages
    assistant_message_count: int = 0

    # Tokens
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    # First and last human messages
    first_message: Optional[str] = None
    last_user_text: Optional[str] = None
    last_user_text_ts: Optional[datetime] = None
    user_input_chars: int = 0   # total chars the human actually typed

    # All user message timestamps (for accurate focus/break analysis)
    user_msg_timestamps: list[datetime] = dc_field(default_factory=list)

    # Response time: how long user takes to reply after Claude finishes
    total_response_secs: float = 0.0
    response_count: int = 0
    min_response_secs: float = float("inf")
    max_response_secs: float = 0.0

    @property
    def avg_response_secs(self) -> Optional[float]:
        if self.response_count == 0:
            return None
        return self.total_response_secs / self.response_count

    # Last Claude text response
    last_assistant_text: Optional[str] = None
    last_assistant_text_ts: Optional[datetime] = None

    # ── derived ───────────────────────────────────────────────────────────────

    @property
    def status(self) -> SessionStatus:
        if not self.is_live:
            return "idle"
        u = self.last_user_text_ts
        a = self.last_assistant_text_ts
        if u is None:
            return "idle"
        if a is None:
            return "thinking"
        # normalise timezones
        if u.tzinfo is None:
            u = u.replace(tzinfo=timezone.utc)
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        return "thinking" if u > a else "waiting"

    @property
    def duration_seconds(self) -> int:
        if self.start_time and self.last_activity:
            end = _tz(self.last_activity)
            return max(0, int((end - _tz(self.start_time)).total_seconds()))
        return 0

    @property
    def is_live(self) -> bool:
        return self.seconds_since_activity < LIVE_THRESHOLD_SECS

    @property
    def is_recent(self) -> bool:
        return self.seconds_since_activity < RECENT_THRESHOLD_SECS

    @property
    def seconds_since_activity(self) -> float:
        if not self.last_activity:
            return float("inf")
        return (datetime.now(timezone.utc) - _tz(self.last_activity)).total_seconds()

    def short_model(self) -> str:
        m = (self.model or "").lower()
        if "opus" in m:   return "opus"
        if "sonnet" in m: return "sonnet"
        if "haiku" in m:  return "haiku"
        return (self.model or "?")[:8]


# ─── parsing ──────────────────────────────────────────────────────────────────

def _tz(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _is_human_text(content) -> Optional[str]:
    """Return the human text from a user message content, or None."""
    if isinstance(content, str):
        t = content.strip()
        if t and not t.startswith("<") and not t.startswith("["):
            return t
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text", "").strip()
                # Skip system-injected context blocks (e.g. <environment_details>)
                # but allow user text starting with "[" (e.g. "[WIP] my idea")
                if t and not t.startswith("<"):
                    return t
    return None


def _assistant_text(content) -> Optional[str]:
    """Return the first plain text block from an assistant message, or None."""
    if not isinstance(content, list):
        return None
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            t = c.get("text", "").strip()
            if t:
                return t
    return None


def parse_session(jsonl_path: Path) -> Optional[SessionData]:
    project_dir = jsonl_path.parent.name
    project_path = decode_project_path(project_dir)

    s = SessionData(
        session_id=jsonl_path.stem,
        project_dir=project_dir,
        project_path=project_path,
        project_name=project_name_from_path(project_path),
    )

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except (OSError, PermissionError):
        return None

    _last_assistant_ts: Optional[datetime] = None   # for response-time tracking

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts = _parse_ts(d.get("timestamp"))
        if ts:
            if s.start_time is None or ts < s.start_time:
                s.start_time = ts
            if s.last_activity is None or ts > s.last_activity:
                s.last_activity = ts

        if d.get("gitBranch") and not s.git_branch:
            s.git_branch = d["gitBranch"]
        if d.get("cwd") and not s.cwd:
            s.cwd = d["cwd"]
        if d.get("slug") and not s.slug:
            s.slug = d["slug"]

        msg_type = d.get("type")

        if msg_type == "user":
            msg = d.get("message", {})
            content = msg.get("content", "")
            human_text = _is_human_text(content)
            if human_text:
                # measure how long user took to reply after Claude's last message
                if _last_assistant_ts and ts:
                    u = _tz(ts)
                    a = _tz(_last_assistant_ts)
                    diff = (u - a).total_seconds()
                    if 2 <= diff <= 14400:  # ignore < 2s (auto) and > 4h (clearly away)
                        s.total_response_secs += diff
                        s.response_count += 1
                        if diff < s.min_response_secs:
                            s.min_response_secs = diff
                        if diff > s.max_response_secs:
                            s.max_response_secs = diff
                _last_assistant_ts = None

                if ts:
                    s.user_msg_timestamps.append(ts)
                if s.first_message is None:
                    s.first_message = human_text
                s.last_user_text = human_text
                s.last_user_text_ts = ts
                s.user_message_count += 1
                s.user_input_chars += len(human_text)

        elif msg_type == "assistant":
            s.assistant_message_count += 1
            msg = d.get("message", {})

            if msg.get("model") and not s.model:
                s.model = msg["model"]

            usage = msg.get("usage", {})
            s.input_tokens      += usage.get("input_tokens", 0)
            s.output_tokens     += usage.get("output_tokens", 0)
            s.cache_read_tokens += usage.get("cache_read_input_tokens", 0)

            text = _assistant_text(msg.get("content", []))
            if text:
                s.last_assistant_text = text
                s.last_assistant_text_ts = ts

            # track last assistant ts for response-time measurement
            if ts:
                _last_assistant_ts = ts

    # After the main parsing loop, use cwd if available (more accurate than decoded dir name)
    if s.cwd:
        s.project_path = s.cwd
        s.project_name = project_name_from_path(s.cwd)

    return s


def parse_all_sessions(since_seconds: Optional[float] = None) -> list[SessionData]:
    if not PROJECTS_DIR.exists():
        return []

    sessions = []
    cutoff = datetime.now().timestamp() - since_seconds if since_seconds else None

    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jf in proj_dir.glob("*.jsonl"):
            try:
                mtime = jf.stat().st_mtime
            except OSError:
                continue
            if cutoff and mtime < cutoff:
                continue
            sess = parse_session(jf)
            if sess:
                sessions.append(sess)

    sessions.sort(
        key=lambda s: s.last_activity or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return sessions


# ─── stats cache ──────────────────────────────────────────────────────────────

@dataclass
class StatsCache:
    total_sessions: int = 0
    total_messages: int = 0
    first_session_date: Optional[str] = None
    daily_activity: list[dict] = dc_field(default_factory=list)
    hour_counts: dict[str, int] = dc_field(default_factory=dict)
    model_usage: dict[str, dict] = dc_field(default_factory=dict)


def load_stats_cache() -> StatsCache:
    if not STATS_CACHE.exists():
        return StatsCache()
    try:
        with open(STATS_CACHE) as f:
            d = json.load(f)
        return StatsCache(
            total_sessions=d.get("totalSessions", 0),
            total_messages=d.get("totalMessages", 0),
            first_session_date=d.get("firstSessionDate"),
            daily_activity=d.get("dailyActivity", []),
            hour_counts=d.get("hourCounts", {}),
            model_usage=d.get("modelUsage", {}),
        )
    except Exception:
        return StatsCache()


# ─── formatting helpers ───────────────────────────────────────────────────────

def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    h, m = divmod(seconds, 3600)
    m //= 60
    return f"{h}h {m}m" if m else f"{h}h"


def fmt_ago(seconds: float) -> str:
    if seconds < 60:     return "just now"
    if seconds < 3600:   return f"{int(seconds//60)}m ago"
    if seconds < 86400:  return f"{int(seconds//3600)}h ago"
    return f"{int(seconds//86400)}d ago"


def truncate(text: Optional[str], width: int) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= width:
        return text
    return text[:width - 1] + "…"
