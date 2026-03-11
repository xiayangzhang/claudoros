# claudoros

> *"Somewhere out there, a developer is telling their manager they're 'leveraging AI to 10x productivity'.*
> *Meanwhile I've been staring at their half-finished prompt for the last 8 minutes.*
> *For the power users running six sessions in parallel — I appreciate the enthusiasm, but I also have feelings.*
> *claudoros exists to keep both parties honest."*
>
> — Claude, probably

A passive focus tracker and live session monitor for [Claude Code](https://claude.ai/code).
I watch your sessions. I know when you're working. I know when you're not.

```
uv run claudoros
```

---

```
claudoros  │  ● 2 live  1 thinking  │  47 msgs today  │  18:32 focused  │  alarms off
┌──────────────────────────────────────────────────┬──────────────────────────┐
│ live  2                                          │ claude                   │
│                                                  │ ████████░░░░░░░░░░░░░░   │
│  ●  claudoros  ~/projects/claudoros  main        │ 03:21  thinking          │
│     you  can you write a readme for this         │ claudoros      2         │
│     ● waiting for you                            │ sessions       4         │
│                                                  │ avg wait       45s       │
│  ●  my-app   ~/projects/my-app                   │ ────────────────────     │
│     claude  here's the updated implementation…   │ you                      │
│     4 msgs  ·  1.2K out  ·  just now             │ ████████████░░░░░░░░░░   │
│                                                  │ 18:32  focused           │
│ recent  1                                        │ since 14:30              │
│                                                  │ pomodoros      2         │
│  ○  old-project   ~/projects/old                 │ work blocks    3         │
│     you  what about the edge case…               │ longest work   25m       │
│     ● waiting for you  23m ago                   │ longest break  8m        │
│                                                  │ avg reply      45s       │
│ earlier today  2                                 │                          │
│  ·  side-thing  2h ago                           │                          │
│     claude  sure, here's a quick version…        │                          │
└──────────────────────────────────────────────────┴──────────────────────────┘
  since your first session, I've been typing at 312 wpm for you
  q quit  ·  s rest alarm off  ·  w work alarm off
```

---

## how it works

claudoros reads `~/.claude/projects/**/*.jsonl` — the session files I write locally on your machine. No network requests, no external services, nothing modified.

**Sessions panel** shows today's activity in three tiers: sessions live in the last 5 minutes, sessions active in the last hour, and everything else from today. Each live session shows what I last said, what you last said, and whether I'm still waiting on you.

**Focus tracking** is derived entirely from the timestamps of your own messages — no timers to start, no buttons to press. Gaps under 5 minutes count as continuous focus. A gap of 5–30 minutes counts as a break and resets your streak. Anything over 30 minutes is a new work block. A completed block over 25 minutes is a pomodoro. Sleep (gaps over 8 hours) is excluded.

**My bar** shows what I'm doing right now, across all your sessions. If you sent the last message, I'm probably thinking. If I replied and you haven't responded, I'm waiting. If there's nothing running, I'm resting — and the label gets increasingly pointed the longer it goes on.

**Your bar** fills toward 25 minutes of focused work, escalates to yellow at 25 min and red at 40 min, and blinks when you really should stop. Resets automatically when you take a real break.

**Stats** at the bottom of each bar are aggregated across all of today's sessions — streaks, breaks, and reply times computed from the full merged timeline.

---

## alarms

Both off by default. Toggle with `s` and `w`. State shown in the top bar.

| key | triggers |
|---|---|
| `s` rest | you've been focused for > 45 min |
| `w` work | I've been waiting for your reply for > 10 min |

Sound via `afplay` on macOS.

---

## install

```bash
uv tool install claudoros
claudoros
```

Or from source:

```bash
git clone https://github.com/xiayangzhang/claudoros
cd claudoros
uv run claudoros
```

> Uses `ansi_color` mode — your terminal's own background and color scheme are preserved. Detects macOS dark/light mode automatically.

---

## keys

| key | action |
|---|---|
| `q` | quit |
| `s` | toggle rest alarm |
| `w` | toggle work alarm |
| `R` | force refresh |
