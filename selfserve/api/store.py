"""store.py - the small amount of state the checkup keeps on its own disk (SQLite, stdlib).

What is kept:
  * scans: one row per finished report or monitoring re-check (order id, plan, site
    host name, grade, score, the problem ids and titles, the ids of checks that were
    not measured, and what changed since the previous counted scan). This is the
    history on the buyer's dashboard and a richer copy of the monitoring baseline.
    Scans of one-time plans (checkup, pro) are deleted 30 days after they were made,
    as the privacy page promises.
  * emails: one row per transactional email key already sent (for example
    "report_ready:<order>" or "monitor:<subscription>:<YYYY-MM>"), so an email
    goes out at most once while this disk lasts.
  * runs: one row per monitoring run with its counts, for the admin page.

No buyer email address, token or payment data is stored here.

This disk is temporary, plainly: on Render's free plan it is wiped on every
redeploy and restart. Nothing that matters depends on it:
  * money data (what was bought, paid, refunded, which subscriptions are current)
    always comes from Square, never from this file;
  * the monthly-monitoring marker (when the site was last scanned for the monthly
    email, which month was emailed, and the problem ids of that scan) and the
    "report ready email was sent" mark live on the Square order's metadata
    (square_api.set_monitor_state / mark_ready_mailed), so a wiped disk never
    means a second monthly email or a second report email.
Losing this file means a shorter dashboard history and plainer titles in the next
monthly email; it never changes what a buyer paid for or how often they are emailed.

Location: APEX_DATA_DIR (default <system temp>/apex-data), file apex.sqlite3.
The folder is made private to this user (0700) and the file 0600.
Thread-safe: one connection shared behind a lock (WAL journal).
"""
import json
import os
import sqlite3
import tempfile
import threading
import time

HISTORY_MAX = 24
ONE_TIME_KEEP_S = 30 * 24 * 3600          # one-time plan scans are deleted after 30 days
PURGE_EVERY_S = 3600
COUNTED_SOURCES = ("report", "monitor")   # "view" (a buyer re-opening the report) never counts


def data_dir():
    return os.environ.get("APEX_DATA_DIR") or os.path.join(tempfile.gettempdir(), "apex-data")


def _private_dir(d):
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.stat(d)
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise PermissionError("the data folder belongs to another user")
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass


def _private_file(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class Store:
    def __init__(self, path=None):
        if path is None:
            d = data_dir()
            _private_dir(d)
            path = os.path.join(d, "apex.sqlite3")
        else:
            _private_dir(os.path.dirname(os.path.abspath(path)))
        _private_file(path)
        self.path = path
        self._lock = threading.Lock()
        self._purged = 0.0
        self._db = sqlite3.connect(path, timeout=10, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    site TEXT NOT NULL DEFAULT '',
                    at REAL NOT NULL,
                    grade TEXT,
                    score INTEGER,
                    source TEXT NOT NULL DEFAULT 'report',
                    issues TEXT NOT NULL DEFAULT '[]',
                    changes TEXT
                );
                CREATE INDEX IF NOT EXISTS scans_order ON scans(order_id, at);
                CREATE TABLE IF NOT EXISTS emails (
                    key TEXT PRIMARY KEY,
                    at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started REAL NOT NULL,
                    finished REAL,
                    counts TEXT
                );
            """)
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(scans)")}
            for name, ddl in (("plan", "TEXT"), ("not_measured", "TEXT NOT NULL DEFAULT '[]'"),
                              ("counted", "INTEGER NOT NULL DEFAULT 1")):
                if name not in cols:
                    self._db.execute(f"ALTER TABLE scans ADD COLUMN {name} {ddl}")
        for extra in (path + "-wal", path + "-shm"):
            if os.path.exists(extra):
                try:
                    os.chmod(extra, 0o600)
                except OSError:
                    pass
        self.purge_old()

    def close(self):
        with self._lock:
            self._db.close()

    # ---- scans -------------------------------------------------------------
    def add_scan(self, order_id, site, grade, score, issues, *, source="report", changes=None, at=None,
                 plan=None, not_measured=None, counted=True):
        """Save one scan. source: "report" (the purchase report), "monitor" (a monthly re-check)
        or "view" (the buyer re-opened the report; shown in history, never a baseline).
        Returns the row id."""
        at = time.time() if at is None else at
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO scans(order_id, site, at, grade, score, source, issues, changes, plan,"
                " not_measured, counted) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, site or "", at, grade, score, source, json.dumps(issues or []),
                 json.dumps(changes) if changes is not None else None, plan,
                 json.dumps(list(not_measured or [])), 1 if counted else 0))
            rid = cur.lastrowid
        if time.time() - self._purged > PURGE_EVERY_S:
            self.purge_old()
        return rid

    def mark_counted(self, scan_id):
        """A monitoring scan became the monthly scan (its email went out, or email is off)."""
        with self._lock:
            self._db.execute("UPDATE scans SET counted=1 WHERE id=?", (scan_id,))

    def purge_old(self, now=None):
        """Delete one-time-plan scans (checkup, pro, and old rows with no plan) older than 30 days."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._db.execute("DELETE FROM scans WHERE COALESCE(plan, 'checkup') != 'monitor' AND at < ?",
                                   (now - ONE_TIME_KEEP_S,))
            self._purged = time.time()
            return cur.rowcount

    def last_scan(self, order_id, sources=None, counted_only=False):
        """The newest scan for this order (optionally only of these sources / only counted ones),
        with its issues, not-measured ids and changes, or None."""
        sql, args = "SELECT * FROM scans WHERE order_id=?", [order_id]
        if sources:
            sql += " AND source IN (%s)" % ",".join("?" * len(sources))
            args += list(sources)
        if counted_only:
            sql += " AND counted=1"
        with self._lock:
            r = self._db.execute(sql + " ORDER BY at DESC, id DESC LIMIT 1", args).fetchone()
        return self._row(r) if r else None

    def baseline(self, order_id):
        """The newest scan that counts for monitoring: the purchase report or a monthly scan
        that was emailed (or recorded with email off). Buyer views never count."""
        return self.last_scan(order_id, sources=COUNTED_SOURCES, counted_only=True)

    def scan_near(self, order_id, at, within=2.0):
        """The counted scan made at time `at` (the one Square's monitoring marker points to), or None."""
        with self._lock:
            r = self._db.execute("SELECT * FROM scans WHERE order_id=? AND counted=1 AND at BETWEEN ? AND ? "
                                 "ORDER BY id DESC LIMIT 1", (order_id, at - within, at + within)).fetchone()
        return self._row(r) if r else None

    def titles(self, order_id):
        """{issue id: title} from every saved scan of this order (for nicer monthly emails)."""
        with self._lock:
            rows = self._db.execute("SELECT issues FROM scans WHERE order_id=? ORDER BY at", (order_id,)).fetchall()
        out = {}
        for r in rows:
            for i in json.loads(r["issues"] or "[]"):
                k = i.get("id") or ("title:" + (i.get("title") or ""))
                if i.get("title"):
                    out[k] = i
        return out

    def history(self, order_id, limit=HISTORY_MAX):
        """[{at, grade, score}] newest first, at most `limit` (max 24)."""
        limit = max(1, min(int(limit), HISTORY_MAX))
        with self._lock:
            rows = self._db.execute("SELECT at, grade, score FROM scans WHERE order_id=? "
                                    "ORDER BY at DESC, id DESC LIMIT ?", (order_id, limit)).fetchall()
        return [{"at": r["at"], "grade": r["grade"], "score": r["score"]} for r in rows]

    def latest_changes(self, order_id):
        """What changed at the newest monthly or purchase scan (None if it has no comparison).
        A buyer re-opening the report never hides the latest monthly changes."""
        s = self.last_scan(order_id, sources=COUNTED_SOURCES)
        return s["changes"] if s else None

    @staticmethod
    def _row(r):
        d = dict(r)
        d["issues"] = json.loads(d.get("issues") or "[]")
        d["not_measured"] = json.loads(d.get("not_measured") or "[]")
        d["changes"] = json.loads(d["changes"]) if d.get("changes") else None
        return d

    # ---- email idempotency -------------------------------------------------
    def claim_email(self, key, at=None):
        """True exactly once per key: the caller may send. False if already claimed."""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO emails(key, at) VALUES (?, ?)",
                                   (key, time.time() if at is None else at))
            return cur.rowcount == 1

    def release_email(self, key):
        """Undo a claim when the send failed, so a later try can send it."""
        with self._lock:
            self._db.execute("DELETE FROM emails WHERE key=?", (key,))

    def email_sent(self, key):
        with self._lock:
            return self._db.execute("SELECT 1 FROM emails WHERE key=?", (key,)).fetchone() is not None

    # ---- monitoring runs ---------------------------------------------------
    def start_run(self, at=None):
        with self._lock:
            cur = self._db.execute("INSERT INTO runs(started) VALUES (?)", (time.time() if at is None else at,))
            return cur.lastrowid

    def finish_run(self, run_id, counts, at=None):
        with self._lock:
            self._db.execute("UPDATE runs SET finished=?, counts=? WHERE id=?",
                             (time.time() if at is None else at, json.dumps(counts), run_id))

    def last_run(self):
        """{started, finished, counts} of the newest monitoring run, or None."""
        with self._lock:
            r = self._db.execute("SELECT started, finished, counts FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if not r:
            return None
        return {"started": r["started"], "finished": r["finished"],
                "counts": json.loads(r["counts"]) if r["counts"] else None}


_default = None
_default_lock = threading.Lock()


def default():
    """The process-wide store at APEX_DATA_DIR (opened on first use)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Store()
        return _default


def set_default(s):
    """Tests only: use this Store (or None to reopen from APEX_DATA_DIR next time)."""
    global _default
    with _default_lock:
        _default = s
