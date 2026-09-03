# Agent Session Board

A tiny localhost dashboard to triage your AI coding-agent sessions — **Claude Code** and
**Kiro-cli** — in one place. It answers two questions that neither tool answers on its own:

- **Which live session should I focus on right now?** (priority + busy/idle/waiting status)
- **Which past sessions did I start and never close out?** (resolved / unresolved)

It reads your existing session transcripts **read-only** and keeps its own triage metadata
(priority, resolved, notes) in a separate SQLite database — it never writes back into
`~/.claude` or `~/.kiro`.

<sub>Stdlib-only Python 3 + a single HTML file. No pip installs, no build step, no external services.</sub>

---

## Features

- **Active column** — currently-running sessions, sorted by priority, with live status
  (`busy` / `idle` / `waiting-on-you`) and how long since the last update.
- **History column** — past sessions, tabbed by *needs-attention / unresolved / resolved / all*.
- **Priority + resolved tagging** — mark High/Medium/Low and resolved/unresolved per session;
  your tags are yours and are preserved across re-scans.
- **Full-text search** over titles, prompts, project, and conversation content (SQLite FTS5).
- **Resume** — one click copies the right resume command for the session's agent
  (`claude --resume <id>` or `kiro-cli chat --resume-id <id>`).
- **Two agents, one board** — Claude Code and Kiro-cli sessions sit side by side; a small
  badge marks the Kiro ones.

## Requirements

- **Python 3** (standard library only — nothing to `pip install`).
- **Linux** — liveness detection reads `/proc` for the agent's process.
- Claude Code sessions under `~/.claude/` and/or Kiro-cli sessions under `~/.kiro/`.

## Quick start

```bash
git clone https://github.com/ashishnitinpatil/agent-session-board.git
cd agent-session-board
python3 server.py
```

Then open the URL it prints (default **http://127.0.0.1:8787/**). The board binds to
localhost only and is guarded by a token that is embedded into the page it serves, so just
opening the printed URL in a browser works.

To do a full re-scan of all transcripts and print stats without starting the server:

```bash
python3 server.py --reindex
```

## Configuration

Both are environment variables:

| Variable     | Default            | Purpose                                             |
|--------------|--------------------|-----------------------------------------------------|
| `CSB_PORT`   | `8787`             | Port to serve on.                                   |
| `CSB_TOKEN`  | random per launch  | Auth token. Set a stable one if you run it as a service so open tabs keep working across restarts. |

## Run it as a background service (optional)

A systemd **user** service keeps it running and starts it at boot. Create
`~/.config/systemd/user/agent-session-board.service`:

```ini
[Unit]
Description=Agent Session Board (localhost dashboard)

[Service]
Environment=CSB_PORT=8026
Environment=CSB_TOKEN=change-me-to-a-stable-random-string
ExecStart=/usr/bin/python3 %h/bin/session-board/server.py
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now agent-session-board
loginctl enable-linger "$USER"   # start at boot without logging in
```

Manage with `systemctl --user {status,restart,stop} agent-session-board` and follow logs with
`journalctl --user -u agent-session-board -f`. `index.html` is served fresh per request (a
browser reload picks up frontend edits); changes to `server.py` need a service restart.

## Data & privacy

- **Read-only sources**: Claude Code transcripts in `~/.claude/`, Kiro-cli sessions in
  `~/.kiro/`. The board never modifies them.
- **Its own store**: `~/.local/share/claude-session-board/board.db` (SQLite) holds the
  cache, search index, and your priority/resolved/notes. This lives outside the repo.
- **Localhost only**: binds `127.0.0.1`, checks the `Host` header, and requires a token —
  nothing is exposed to the network.
- **No LLM re-feed, no auto-exec**: transcript text is only ever displayed (control/ANSI
  characters stripped, rendered as text so nothing in a transcript can execute); "resume"
  copies a command for you to run — it never runs anything itself.

## How it works

- `server.py` scans the transcript files, parses each session into a row (title, project,
  timestamps, message counts, git state), and caches it in `board.db`. Live sessions are
  detected from each agent's live-session file plus a `/proc` liveness check.
- `index.html` is a single dependency-free page that polls a small JSON API
  (`/api/active`, `/api/history`, `/api/search`, …) and renders the two columns.
- The `?` button in the header documents the non-obvious behaviours (title refresh,
  sorting, priority defaults, how triage never touches your manual tags).

## Repository layout

```
server.py     # the whole backend: transcript parsing, SQLite cache, JSON API, HTTP server
index.html    # the whole frontend: one page, no framework, no bundler
```

---

*Personal utility, shared as-is.*
