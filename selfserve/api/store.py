"""store.py - the small amount of state the checkup keeps on its own disk (SQLite, stdlib).

What is kept:
  * scans: one row per finished report or monitoring re-check (order id, site
    host name, grade, score, the problem list, and what changed since the
    previous scan). This is the history on the buyer's dashboard and the
    baseline for monitoring diffs.
  * emails: one row per transactional email key already sent (for example
    "report_ready:<order>" or "monitor:<subscription>:<YYYY-MM>"), so an email
    goes out at most once even when a webhook or a run repeats.
  * runs: one row per monitoring run with its counts, for the admin page.

No buyer email address, token or payment data is stored here.

Best effort, plainly: on Render's free plan this disk is wiped on every
redeploy and restart, so scan history and "already emailed" marks can be lost.
Money data (what was bought, paid, refunded, which subscriptions are active)
always comes from Square, never from this file. Losing it means a shorter
history and, at worst, one repeated email; it never changes what a buyer paid
for.

Location: APEX_DATA_DIR (default <system temp>/apex-data), file apex.sqlite3.
Thread-safe: one connection shared behind a lock (WAL journal).
"""
import json
import os
import sqlite3
import tempfile
import threading
import time

HISTORY_MAX = 24


def data_dir():
    return os.environ.get("APEX_DATA_DIR") or os.path.join(tempfile.gettempdir(), "apex-data")


class Store:
    def __init__(self, path=None):
        if path is None:
            d = data_dir()
            os.makedirs(d, mode=0o700, exist_ok=True)
            path = os.path.join(d, "apex.sqlite3")
        else:
            os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
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

    def close(self):
        with self._lock:
            self._db.close()

    # ---- scans -------------------------------------------------------------
    def add_scan(self, order_id, site, grade, score, issues, *, source="report", changes=None, at=None):
        at = time.time() if at is None else at
        with self._lock:
            self._db.execute(
                "INSERT INTO scans(order_id, site, at, grade, score, source, issues, changes) VALUES (?,?,?,?,?,?,?,?)",
                (order_id, site or "", at, grade, score, source, json.dumps(issues or []),
                 json.dumps(changes) if changes is not None else None))

    def last_scan(self, order_id):
        """The newest scan for this order, with its issues and changes, or None."""
        with self._lock:
            r = self._db.execute("SELECT * FROM scans WHERE order_id=? ORDER BY at DESC, id DESC LIMIT 1",
                                 (order_id,)).fetchone()
        return self._row(r) if r else None

    def history(self, order_id, limit=HISTORY_MAX):
        """[{at, grade, score}] newest first, at most `limit` (max 24)."""
        limit = max(1, min(int(limit), HISTORY_MAX))
        with self._lock:
            rows = self._db.execute("SELECT at, grade, score FROM scans WHERE order_id=? "
                                    "ORDER BY at DESC, id DESC LIMIT ?", (order_id, limit)).fetchall()
        return [{"at": r["at"], "grade": r["grade"], "score": r["score"]} for r in rows]

    def latest_changes(self, order_id):
        """What changed at the newest scan (None if the newest scan has no comparison)."""
        s = self.last_scan(order_id)
        return s["changes"] if s else None

    @staticmethod
    def _row(r):
        d = dict(r)
        d["issues"] = json.loads(d.get("issues") or "[]")
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
