#!/usr/bin/env python3
"""Claude Session Board — a localhost dashboard to triage active + past Claude Code sessions.

Read-only over ~/.claude. Your own metadata (priority / resolved / note) lives in a
separate SQLite DB and is NEVER written back into ~/.claude.

Run:   python3 ~/bin/session-board/server.py         # index + serve on 127.0.0.1:8787
       python3 ~/bin/session-board/server.py --reindex   # full reindex, print stats, exit
Env:   CSB_PORT (default 8787)
"""
import os, sys, re, glob, json, time, calendar, secrets, sqlite3, subprocess, urllib.parse, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME     = os.path.expanduser("~")
CLAUDE   = os.path.join(HOME, ".claude")
PROJECTS = os.path.join(CLAUDE, "projects")
SESSIONS = os.path.join(CLAUDE, "sessions")
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local", "share")),
                        "claude-session-board")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH  = os.path.join(DATA_DIR, "board.db")
HERE     = os.path.dirname(os.path.abspath(__file__))
INDEX    = os.path.join(HERE, "index.html")
PORT     = int(os.environ.get("CSB_PORT", "8787"))
TOKEN    = os.environ.get("CSB_TOKEN") or secrets.token_urlsafe(18)
GIT_TTL  = 30  # seconds

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
SKIP_PREFIXES = ("<command-", "<local-command", "<system-reminder", "Caveat:", "<bash-")


def clean(s, n=200):
    """Sanitize any transcript-derived text: strip ANSI/control chars, collapse ws, truncate."""
    if not s:
        return ""
    if not isinstance(s, str):
        s = str(s)
    s = ANSI_RE.sub("", s)
    s = CTRL_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:n].rstrip() + "…") if len(s) > n else s


def epoch_utc(ts):
    """Parse an ISO-8601 UTC timestamp (…Z) to epoch seconds. None on failure.

    Transcript timestamps are UTC; time.mktime would wrongly read them as local.
    """
    if not ts:
        return None
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def ms_to_iso(ms):
    """Epoch milliseconds (live session <pid>.json) -> ISO-8601 UTC string, or None."""
    if not ms:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))
    except Exception:
        return None


# ---------------------------------------------------------------- transcript parsing
def _user_text(o):
    if o.get("isMeta"):
        return None
    m = o.get("message") or {}
    c = m.get("content")
    txt = None
    if isinstance(c, str):
        txt = c
    elif isinstance(c, list):
        parts = []
        for p in c:
            if not isinstance(p, dict):
                continue
            if p.get("type") in ("tool_result", "tool_use", "image"):
                return None  # not a human prompt turn
            if p.get("type") == "text" and p.get("text"):
                parts.append(p["text"])
        txt = " ".join(parts) if parts else None
    if not txt:
        return None
    ts = txt.strip()
    if any(ts.startswith(p) for p in SKIP_PREFIXES):
        return None
    return ts


def _assistant_text(o):
    """Assistant text blocks only (skip tool_use/thinking); '' if none."""
    m = o.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p["text"] for p in c
                        if isinstance(p, dict) and p.get("type") == "text" and p.get("text"))
    return ""


CONTENT_CAP = 120000  # chars of conversation text indexed per session (bounded for FTS)


def parse_session(path):
    sid = cwd = branch = slug = None
    ai_titles = []   # ordered, de-duped history of aiTitle values (Claude regenerates it as topics shift)
    first_user = last_prompt = started = last = last_msg = version = None
    msg_count = user_msgs = 0
    content_parts, content_len = [], 0
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                sid = o.get("sessionId") or o.get("session_id") or sid
                if o.get("cwd"):        cwd = o["cwd"]
                if o.get("gitBranch"):  branch = o["gitBranch"]
                if o.get("version"):    version = o["version"]
                if o.get("aiTitle") and (not ai_titles or ai_titles[-1] != o["aiTitle"]):
                    ai_titles.append(o["aiTitle"])
                if o.get("slug") and not slug: slug = o["slug"]
                if o.get("lastPrompt"): last_prompt = o["lastPrompt"]
                ts = o.get("timestamp")
                if ts:
                    if started is None:
                        started = ts
                    last = ts
                t = o.get("type")
                if t in ("user", "assistant"):
                    msg_count += 1
                    if ts:            # true conversational recency (ignores trailing system/pr-link/attachment entries)
                        last_msg = ts
                if t == "user":
                    txt = _user_text(o)
                    if txt:
                        user_msgs += 1
                        if first_user is None:
                            first_user = txt
                        if content_len < CONTENT_CAP:
                            content_parts.append(txt)
                            content_len += len(txt)
                elif t == "assistant" and content_len < CONTENT_CAP:
                    atxt = _assistant_text(o)
                    if atxt:
                        content_parts.append(atxt)
                        content_len += len(atxt)
    except Exception:
        pass
    sid = sid or os.path.splitext(os.path.basename(path))[0]
    if ai_titles:
        title, src = ai_titles[-1], "ai"
    elif first_user:
        title, src = first_user, "prompt"
    elif slug:
        title, src = slug.replace("-", " "), "slug"
    else:
        title, src = "(untitled)", "none"
    title = clean(title, 180)
    # earlier aiTitles this session outgrew, most-recent-first — so a name we outgrew is never lost
    prev_titles, seen = [], {title}
    for t in reversed(ai_titles[:-1]):
        t = clean(t, 180)
        if t and t not in seen:
            seen.add(t); prev_titles.append(t)
    return dict(
        session_id=sid, cwd=cwd or "", branch=branch or "",
        title=title, title_source=src, prev_titles=prev_titles[:6],
        started=started, last=last, last_msg=last_msg, msg_count=msg_count, user_msgs=user_msgs,
        version=version or "", last_prompt=clean(last_prompt or first_user or "", 220),
        content=clean(" ".join(content_parts), CONTENT_CAP),
    )


# ---------------------------------------------------------------- db
def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS session(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT UNIQUE, project TEXT, cwd TEXT, title TEXT, title_source TEXT,
      git_branch TEXT, started_at TEXT, last_activity TEXT, last_msg TEXT,
      msg_count INTEGER, user_msgs INTEGER, version TEXT,
      last_prompt TEXT, file_path TEXT, file_mtime REAL
    );
    CREATE TABLE IF NOT EXISTS meta(
      session_id TEXT PRIMARY KEY, priority INTEGER, resolved INTEGER DEFAULT 0,
      resolved_at TEXT, note TEXT, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS gitcache(
      cwd TEXT PRIMARY KEY, is_repo INTEGER, branch TEXT, dirty INTEGER, ahead INTEGER, checked_at REAL
    );
    CREATE TABLE IF NOT EXISTS session_content(
      session_id TEXT PRIMARY KEY, content TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS session_fts USING fts5(
      title, last_prompt, project, content, tokenize='porter'
    );
    """)
    # migrate older DBs that predate a column (ADD COLUMN is a no-op error if it exists)
    for col, decl in (("last_msg", "TEXT"), ("prev_titles", "TEXT")):
        try:
            c.execute("ALTER TABLE session ADD COLUMN %s %s" % (col, decl))
        except Exception:
            pass
    c.commit()


def project_name(cwd):
    if not cwd:
        return "(none)"
    base = os.path.basename(cwd.rstrip("/")) or cwd
    return base


def is_sidechain_file(path):
    """True for subagent/Task side-transcripts (named by a leaf uuid, not a real session).

    Every line of such a file has isSidechain=true; the first parseable line is enough.
    """
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    return bool(json.loads(line).get("isSidechain"))
                except Exception:
                    continue
    except OSError:
        pass
    return False


def _upsert_session(c, s, path, mt):
    """Write one parsed session into the session + session_content tables (title, prev_titles, etc)."""
    prev_titles_json = json.dumps(s["prev_titles"], ensure_ascii=False) if s.get("prev_titles") else None
    c.execute("""INSERT INTO session
      (session_id,project,cwd,title,title_source,prev_titles,git_branch,started_at,last_activity,last_msg,
       msg_count,user_msgs,version,last_prompt,file_path,file_mtime)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(session_id) DO UPDATE SET
       project=excluded.project,cwd=excluded.cwd,title=excluded.title,title_source=excluded.title_source,
       prev_titles=excluded.prev_titles,
       git_branch=excluded.git_branch,started_at=excluded.started_at,last_activity=excluded.last_activity,
       last_msg=excluded.last_msg,
       msg_count=excluded.msg_count,user_msgs=excluded.user_msgs,version=excluded.version,
       last_prompt=excluded.last_prompt,file_path=excluded.file_path,file_mtime=excluded.file_mtime""",
      (s["session_id"], project_name(s["cwd"]), s["cwd"], s["title"], s["title_source"], prev_titles_json, s["branch"],
       s["started"], s["last"], s["last_msg"], s["msg_count"], s["user_msgs"], s["version"], s["last_prompt"], path, mt))
    c.execute("INSERT INTO session_content(session_id,content) VALUES(?,?) "
              "ON CONFLICT(session_id) DO UPDATE SET content=excluded.content",
              (s["session_id"], s["content"]))


_LIVE_PARSE_LOCK = threading.Lock()
_LIVE_PARSE_AT = {}          # session_id -> wall-clock of last parse-on-poll (rate-limit busy reparses)
LIVE_REFRESH_MIN_S = 20      # a continuously-busy session is reparsed at most this often on poll


def _refresh_live(c, live):
    """Parse-on-poll: bring the currently-live sessions' rows up to date so active cards show the
    real aiTitle (not the terse live 'name' fallback) even before the next full reindex. Cheap —
    only stats the ≤~20 live transcripts and reparses the ones whose mtime moved, and even then no
    more than once per LIVE_REFRESH_MIN_S per session (a busy transcript can be 100+ MB)."""
    known = {r["session_id"]: (r["file_path"], r["file_mtime"])
             for r in c.execute("SELECT session_id,file_path,file_mtime FROM session")}
    changed = False
    for d in live:
        sid = d.get("sessionId")
        if not sid:
            continue
        path = known[sid][0] if (sid in known and known[sid][0]) else None
        if not path:  # brand-new session never indexed — locate its transcript
            hits = glob.glob(os.path.join(PROJECTS, "**", sid + ".jsonl"), recursive=True)
            path = hits[0] if hits else None
        if not path or not os.path.exists(path):
            continue
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        if sid in known and known[sid][1] == mt:
            continue  # unchanged since last parse
        # transcript moved — throttle so a continuously-busy (large) session isn't reparsed every poll
        now = time.time()
        with _LIVE_PARSE_LOCK:
            if now - _LIVE_PARSE_AT.get(sid, 0) < LIVE_REFRESH_MIN_S:
                continue                       # parsed recently; skip (mtime stays stale → caught next window)
            _LIVE_PARSE_AT[sid] = now          # claim the slot before the slow parse so concurrent polls skip
        if is_sidechain_file(path):
            continue
        s = parse_session(path)
        if s["session_id"]:
            _upsert_session(c, s, path, mt)
            changed = True
    if changed:
        c.commit()   # FTS not rebuilt here (search catches up on next reindex); titles are what matter live


def reindex(c, full=False):
    files = glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True)
    known = {r["file_path"]: r["file_mtime"] for r in c.execute("SELECT file_path,file_mtime FROM session")}
    changed = 0
    scanned = 0
    for path in files:
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        if not full and known.get(path) == mt:
            continue
        if is_sidechain_file(path):
            continue  # subagent side-transcript, not a user-facing session
        scanned += 1
        s = parse_session(path)
        if not s["session_id"]:
            continue
        _upsert_session(c, s, path, mt)
        changed += 1
    if changed or full:
        c.execute("DELETE FROM session_fts")
        c.execute("INSERT INTO session_fts(rowid,title,last_prompt,project,content) "
                  "SELECT s.id, s.title||' '||COALESCE(s.prev_titles,''), s.last_prompt, s.project, COALESCE(sc.content,'') "
                  "FROM session s LEFT JOIN session_content sc ON sc.session_id=s.session_id")
    c.commit()
    return changed, len(files)


# ---------------------------------------------------------------- git + liveness
def compute_git(cwd):
    if not cwd or not os.path.isdir(cwd):
        return dict(is_repo=0, branch="", dirty=0, ahead=0)

    def run(args):
        try:
            return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except Exception:
            return ""
    if run(["rev-parse", "--is-inside-work-tree"]) != "true":
        return dict(is_repo=0, branch="", dirty=0, ahead=0)
    branch = run(["rev-parse", "--abbrev-ref", "HEAD"])
    dirty = len([l for l in run(["status", "--porcelain"]).splitlines() if l.strip()])
    ahead_raw = run(["rev-list", "--count", "@{u}.."])
    ahead = int(ahead_raw) if ahead_raw.isdigit() else 0
    return dict(is_repo=1, branch=branch, dirty=dirty, ahead=ahead)


def git_for(c, cwds):
    """Return {cwd: git-dict}, refreshing gitcache (TTL) for the given cwds."""
    now = time.time()
    out = {}
    for cwd in set(x for x in cwds if x):
        row = c.execute("SELECT * FROM gitcache WHERE cwd=?", (cwd,)).fetchone()
        if row and now - row["checked_at"] < GIT_TTL:
            out[cwd] = dict(is_repo=row["is_repo"], branch=row["branch"],
                            dirty=row["dirty"], ahead=row["ahead"])
            continue
        sig = compute_git(cwd)
        c.execute("INSERT OR REPLACE INTO gitcache(cwd,is_repo,branch,dirty,ahead,checked_at) "
                  "VALUES(?,?,?,?,?,?)",
                  (cwd, sig["is_repo"], sig["branch"], sig["dirty"], sig["ahead"], now))
        out[cwd] = sig
    c.commit()
    return out


def proc_alive(pid):
    """(alive, starttime) — starttime is field 22 of /proc/<pid>/stat, robust to spaces in comm."""
    try:
        with open("/proc/%d/stat" % int(pid)) as fh:
            data = fh.read()
        rest = data[data.rfind(")") + 2:].split()
        return True, rest[19]  # field 22 (starttime); rest[0] == field 3 (state)
    except Exception:
        return False, None


def read_live():
    live = []
    for f in glob.glob(os.path.join(SESSIONS, "*.json")):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        pid = d.get("pid")
        if not pid:
            continue
        alive, pstart = proc_alive(pid)
        if not alive:
            continue
        if d.get("procStart") and pstart and str(d["procStart"]) != str(pstart):
            continue  # PID was reused by another process
        live.append(d)
    return live


# ---------------------------------------------------------------- state builders
def meta_map(c, ids=None):
    if ids is None:
        rows = c.execute("SELECT * FROM meta").fetchall()
    else:
        rows = []
        for chunk in [list(ids)[i:i + 400] for i in range(0, len(ids), 400)]:
            q = "SELECT * FROM meta WHERE session_id IN (%s)" % ",".join("?" * len(chunk))
            rows += c.execute(q, chunk).fetchall()
    return {r["session_id"]: dict(priority=r["priority"], resolved=r["resolved"],
                                  note=r["note"] or "", resolved_at=r["resolved_at"]) for r in rows}


def build_active(c):
    live = read_live()
    _refresh_live(c, live)   # parse-on-poll so new/renamed live sessions show their real aiTitle
    ids = [d.get("sessionId") for d in live if d.get("sessionId")]
    idx = {r["session_id"]: r for r in c.execute(
        "SELECT * FROM session WHERE session_id IN (%s)" % ",".join("?" * len(ids)), ids)} if ids else {}
    mm = meta_map(c, ids)
    gm = git_for(c, [d.get("cwd") for d in live])
    out = []
    for d in live:
        sid = d.get("sessionId") or ""
        row = idx.get(sid)
        cwd = d.get("cwd") or (row["cwd"] if row else "")
        m = mm.get(sid, {})
        out.append(dict(
            session_id=sid,
            name=clean(d.get("name") or "", 60),
            title=(row["title"] if row else clean(d.get("name") or "", 120)),
            prev_titles=(_prev_titles(row) if row else []),
            cwd=cwd, project=project_name(cwd),
            status=d.get("status") or "unknown",
            pid=d.get("pid"),
            last_activity=(row["last_activity"] if row else None),
            last_msg=(row["last_msg"] if row else None),
            updated_at=ms_to_iso(d.get("updatedAt")),
            status_since=ms_to_iso(d.get("statusUpdatedAt")),
            priority=m.get("priority"), resolved=m.get("resolved", 0), note=m.get("note", ""),
            git=gm.get(cwd, dict(is_repo=0, dirty=0, ahead=0, branch="")),
        ))
    # "waiting" = blocked on human input (permission prompt / question / plan approval) — surface it first
    order = {"waiting": 0, "busy": 1, "idle": 2}
    def _recent(r):  # tie-break within a status/priority group by last update, most-recent first
        return -(epoch_utc(r.get("updated_at") or r.get("status_since") or r.get("last_activity") or "") or 0)
    # no explicit priority sorts the same as High (1) in the active list — an untriaged live session
    # is assumed important until you say otherwise.
    out.sort(key=lambda r: (order.get(r["status"], 3), (r["priority"] or 1), _recent(r), r["name"]))
    waiting = sum(1 for r in out if r["status"] == "waiting")
    busy = sum(1 for r in out if r["status"] == "busy")
    return dict(active=out, stats=dict(active=len(out), waiting=waiting, busy=busy,
                                       idle=len(out) - waiting - busy))


def _prev_titles(r):
    """Parse the stored JSON list of former names; [] on any missing/legacy/garbled value."""
    try:
        v = r["prev_titles"]
    except (IndexError, KeyError):
        return []
    if not v:
        return []
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else []
    except Exception:
        return []


def _row_dict(r, m, git):
    return dict(
        session_id=r["session_id"], title=r["title"], title_source=r["title_source"],
        prev_titles=_prev_titles(r),
        project=r["project"], cwd=r["cwd"], branch=r["git_branch"],
        started_at=r["started_at"], last_activity=r["last_activity"], last_msg=r["last_msg"],
        msg_count=r["msg_count"], user_msgs=r["user_msgs"], version=r["version"],
        last_prompt=r["last_prompt"],
        priority=m.get("priority"), resolved=m.get("resolved", 0), note=m.get("note", ""),
        git=git.get(r["cwd"], dict(is_repo=0, dirty=0, ahead=0, branch="")),
    )


def build_history(c, filt="unresolved", sort="recent", limit=150, offset=0, include_active=False):
    all_rows = c.execute("SELECT * FROM session").fetchall()
    gm = git_for(c, [r["cwd"] for r in all_rows])
    mm = meta_map(c)
    live_ids = {d.get("sessionId") for d in read_live()}
    now = time.time()

    def is_recent(ts, days=7):
        t = epoch_utc(ts)
        return t is not None and (now - t) < days * 86400

    enriched = []
    for r in all_rows:
        m = mm.get(r["session_id"], {})
        d = _row_dict(r, m, gm)
        g = d["git"]
        d["live"] = r["session_id"] in live_ids
        d["attention"] = (not d["resolved"]) and (g["dirty"] > 0 or g["ahead"] > 0 or is_recent(r["last_activity"]))
        enriched.append(d)

    # By default History hides sessions that are currently live (they're already in the Active column);
    # the "include active" toggle brings them back. Drop before counts so tab numbers match the list.
    if not include_active:
        enriched = [d for d in enriched if not d["live"]]

    counts = dict(
        all=len(enriched),
        resolved=sum(1 for d in enriched if d["resolved"]),
        open=sum(1 for d in enriched if not d["resolved"]),
        attention=sum(1 for d in enriched if d["attention"]),
    )
    if filt == "unresolved":
        rows = [d for d in enriched if not d["resolved"]]
    elif filt == "attention":
        rows = [d for d in enriched if d["attention"]]
    elif filt == "resolved":
        rows = [d for d in enriched if d["resolved"]]
    else:
        rows = enriched

    # "recent"/"oldest" rank by last actual message; "updated" keeps the raw last-entry time
    def msg_ts(d):
        return d.get("last_msg") or d["last_activity"] or ""
    def upd_desc(d):  # last update (raw last-entry time), most-recent first — for tie-breaking
        return -(epoch_utc(d.get("last_activity") or d.get("last_msg") or "") or 0)
    if sort == "priority":
        rows.sort(key=lambda d: ((d["priority"] or 9), upd_desc(d)))
    elif sort == "oldest":
        rows.sort(key=lambda d: (msg_ts(d), upd_desc(d)))
    elif sort == "updated":
        rows.sort(key=lambda d: d["last_activity"] or "", reverse=True)
    else:  # recent — by last message
        rows.sort(key=lambda d: msg_ts(d), reverse=True)

    total = len(rows)
    page = rows[offset:offset + limit]
    return dict(rows=page, total=total, counts=counts,
                filtered_ids=[d["session_id"] for d in rows])


def search(c, q, limit=60):
    terms = re.findall(r"\w+", q or "")
    if not terms:
        return dict(rows=[])
    match = " ".join(t + "*" for t in terms)
    try:
        frows = c.execute(
            "SELECT rowid, bm25(session_fts) AS rank FROM session_fts "
            "WHERE session_fts MATCH ? ORDER BY rank LIMIT ?", (match, limit * 3)).fetchall()
    except sqlite3.OperationalError:
        return dict(rows=[])
    if not frows:
        return dict(rows=[])
    ids = [fr["rowid"] for fr in frows]
    srows = {r["id"]: r for r in c.execute(
        "SELECT * FROM session WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}
    gm = git_for(c, [srows[i]["cwd"] for i in ids if i in srows])
    mm = meta_map(c)
    now = time.time()
    scored = []
    for fr in frows:
        r = srows.get(fr["rowid"])
        if not r:
            continue
        # recency boost: penalize age so recent, well-matched sessions rank higher
        age_days = 0.0
        t = epoch_utc(r["last_activity"])
        if t is not None:
            age_days = max(0.0, (now - t) / 86400)
        scored.append((fr["rank"] + age_days * 0.03, r))
    scored.sort(key=lambda x: x[0])
    rows = [_row_dict(r, mm.get(r["session_id"], {}), gm) for _, r in scored[:limit]]
    return dict(rows=rows)


def set_meta(c, session_id, **kw):
    if not session_id:
        return
    row = c.execute("SELECT * FROM meta WHERE session_id=?", (session_id,)).fetchone()
    cur = dict(priority=row["priority"] if row else None,
               resolved=row["resolved"] if row else 0,
               note=row["note"] if row else None,
               resolved_at=row["resolved_at"] if row else None)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    if "priority" in kw:
        cur["priority"] = kw["priority"]
    if "note" in kw:
        cur["note"] = kw["note"]
    if "resolved" in kw:
        newv = 1 if kw["resolved"] else 0
        if newv and not cur["resolved"]:
            cur["resolved_at"] = now_iso
        if not newv:
            cur["resolved_at"] = None
        cur["resolved"] = newv
    c.execute("INSERT INTO meta(session_id,priority,resolved,resolved_at,note,updated_at) "
              "VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
              "priority=excluded.priority,resolved=excluded.resolved,resolved_at=excluded.resolved_at,"
              "note=excluded.note,updated_at=excluded.updated_at",
              (session_id, cur["priority"], cur["resolved"], cur["resolved_at"], cur["note"], now_iso))
    c.commit()


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _host_ok(self):
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def _auth(self):
        return self.headers.get("X-Board-Token") == TOKEN

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            try:
                html = open(INDEX, encoding="utf-8").read().replace("__CSB_TOKEN__", TOKEN)
            except OSError:
                return self._send(500, "index.html missing", "text/plain")
            return self._send(200, html, "text/html; charset=utf-8")
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        c = db()
        try:
            if u.path == "/api/active":
                return self._send(200, build_active(c))
            if u.path == "/api/history":
                return self._send(200, build_history(
                    c, qs.get("filter", ["unresolved"])[0], qs.get("sort", ["recent"])[0],
                    int(qs.get("limit", ["150"])[0]), int(qs.get("offset", ["0"])[0]),
                    include_active=qs.get("active", ["0"])[0] == "1"))
            if u.path == "/api/search":
                return self._send(200, search(c, qs.get("q", [""])[0]))
            return self._send(404, {"error": "not found"})
        finally:
            c.close()

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            body = json.loads(self.rfile.read(length) or "{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        u = urllib.parse.urlparse(self.path)
        c = db()
        try:
            if u.path == "/api/meta":
                kw = {}
                for k in ("priority", "resolved", "note"):
                    if k in body:
                        kw[k] = body[k]
                if "note" in kw:
                    kw["note"] = clean(kw["note"], 500)
                set_meta(c, body.get("session_id"), **kw)
                return self._send(200, {"ok": True})
            if u.path == "/api/meta/bulk":
                ids = body.get("session_ids") or []
                val = 1 if body.get("resolved") else 0
                for sid in ids:
                    set_meta(c, sid, resolved=val)
                return self._send(200, {"ok": True, "count": len(ids)})
            if u.path == "/api/rescan":
                changed, total = reindex(c)
                return self._send(200, {"ok": True, "changed": changed, "total": total})
            return self._send(404, {"error": "not found"})
        finally:
            c.close()


def main():
    c = db()
    init_db(c)
    t0 = time.time()
    changed, files = reindex(c, full="--reindex" in sys.argv)
    dt = time.time() - t0
    nsess = c.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    print("scanned %d transcript files -> %d sessions (%d (re)parsed) in %.2fs" % (files, nsess, changed, dt))
    if "--reindex" in sys.argv:
        n = c.execute("SELECT COUNT(*) FROM session").fetchone()[0]
        by = c.execute("SELECT title_source,COUNT(*) FROM session GROUP BY title_source").fetchall()
        print("sessions in db: %d ; title sources: %s" % (n, {r[0]: r[1] for r in by}))
        c.close()
        return
    c.close()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = "http://127.0.0.1:%d/" % PORT
    print("\n  Claude Session Board")
    print("  %s" % url)
    print("  (localhost only, token-guarded; Ctrl-C to stop)\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
