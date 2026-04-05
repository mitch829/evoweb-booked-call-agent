"""
Queue — tracks bot conversation state per lead.
Uses PostgreSQL (via DATABASE_URL) in production, SQLite locally.
"""

import os
import datetime

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL", "")

# ---------------------------------------------------------------------------
# Connection helper — Postgres or SQLite
# ---------------------------------------------------------------------------

def _get_conn():
    if DATABASE_URL:
        import psycopg2
        return psycopg2.connect(DATABASE_URL), "postgres"
    else:
        import sqlite3, pathlib
        db_path = pathlib.Path(__file__).parent / "queue.db"
        return sqlite3.connect(str(db_path)), "sqlite"


def _ph(db_type):
    """Return the correct placeholder for the DB type."""
    return "%s" if db_type == "postgres" else "?"


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init_db():
    conn, db = _get_conn()
    ph = _ph(db)
    cur = conn.cursor()

    if db == "postgres":
        cur.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                id SERIAL PRIMARY KEY,
                client_id TEXT NOT NULL,
                opportunity_id TEXT NOT NULL,
                contact_id TEXT NOT NULL,
                contact_name TEXT,
                phone TEXT,
                suburb TEXT,
                current_stage TEXT DEFAULT 'POST_CALL',
                current_question TEXT,
                waiting_reply INTEGER DEFAULT 0,
                next_action_at TEXT,
                nudge_at TEXT,
                nudge_count INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                paused INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                opportunity_id TEXT NOT NULL,
                contact_id TEXT NOT NULL,
                contact_name TEXT,
                phone TEXT,
                suburb TEXT,
                current_stage TEXT DEFAULT 'POST_CALL',
                current_question TEXT,
                waiting_reply INTEGER DEFAULT 0,
                next_action_at TEXT,
                nudge_at TEXT,
                nudge_count INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                paused INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)

    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COLUMNS = [
    "id", "client_id", "opportunity_id", "contact_id", "contact_name",
    "phone", "suburb", "current_stage", "current_question", "waiting_reply",
    "next_action_at", "nudge_at", "nudge_count", "retry_count", "paused", "created_at"
]

def row_to_dict(row):
    return dict(zip(COLUMNS, row)) if row else None


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------

def add_to_queue(client_id, opportunity_id, contact_id, contact_name, phone, suburb):
    conn, db = _get_conn()
    ph = _ph(db)
    cur = conn.cursor()

    cur.execute(
        f"SELECT id FROM queue WHERE opportunity_id = {ph} AND current_stage NOT IN ('JOB_WON','JOB_LOST','WON','LOST','WRITEOFF')",
        (opportunity_id,)
    )
    if cur.fetchone():
        cur.close()
        conn.close()
        print(f"  — Lead already in queue (opportunity {opportunity_id}), skipping")
        return False

    cur.execute(f"""
        INSERT INTO queue
        (client_id, opportunity_id, contact_id, contact_name, phone, suburb,
         current_stage, waiting_reply, created_at)
        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, 'POST_CALL', 0, {ph})
    """, (client_id, opportunity_id, contact_id, contact_name, phone, suburb,
          datetime.datetime.utcnow().isoformat()))
    conn.commit()
    cur.close()
    conn.close()
    return True


def get_waiting(client_id):
    conn, db = _get_conn()
    ph = _ph(db)
    cur = conn.cursor()
    cur.execute(f"""
        SELECT * FROM queue
        WHERE client_id = {ph} AND waiting_reply = 1 AND paused = 0
        ORDER BY created_at ASC LIMIT 1
    """, (client_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row_to_dict(row)


def get_next_due(client_id):
    conn, db = _get_conn()
    ph = _ph(db)
    now = datetime.datetime.utcnow().isoformat()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT * FROM queue
        WHERE client_id = {ph} AND waiting_reply = 0 AND paused = 0
        AND next_action_at <= {ph}
        AND current_stage NOT IN ('JOB_WON','JOB_LOST','WON','LOST','WRITEOFF')
        ORDER BY next_action_at ASC LIMIT 1
    """, (client_id, now))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row_to_dict(row)


def get_due_nudges(client_id, max_nudges=2):
    conn, db = _get_conn()
    ph = _ph(db)
    now = datetime.datetime.utcnow().isoformat()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT * FROM queue
        WHERE client_id = {ph} AND waiting_reply = 1 AND paused = 0
        AND nudge_at IS NOT NULL AND nudge_at <= {ph}
        AND nudge_count < {ph}
        AND current_stage NOT IN ('JOB_WON','JOB_LOST','WON','LOST','WRITEOFF')
    """, (client_id, now, max_nudges))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [row_to_dict(r) for r in rows]


def update_queue(lead_id, **kwargs):
    if not kwargs:
        return
    conn, db = _get_conn()
    ph = _ph(db)
    sets = ", ".join(f"{k} = {ph}" for k in kwargs)
    values = list(kwargs.values()) + [lead_id]
    cur = conn.cursor()
    cur.execute(f"UPDATE queue SET {sets} WHERE id = {ph}", values)
    conn.commit()
    cur.close()
    conn.close()


def set_next_action(lead_id, days):
    next_at = (datetime.datetime.utcnow() + datetime.timedelta(days=days)).isoformat()
    update_queue(lead_id, next_action_at=next_at, waiting_reply=0, nudge_count=0, nudge_at=None)


def set_nudge(lead_id, hours):
    nudge_at = (datetime.datetime.utcnow() + datetime.timedelta(hours=hours)).isoformat()
    update_queue(lead_id, nudge_at=nudge_at)


def pause_all(client_id):
    conn, db = _get_conn()
    ph = _ph(db)
    cur = conn.cursor()
    cur.execute(f"UPDATE queue SET paused = 1 WHERE client_id = {ph}", (client_id,))
    conn.commit()
    cur.close()
    conn.close()


def resume_all(client_id):
    conn, db = _get_conn()
    ph = _ph(db)
    cur = conn.cursor()
    cur.execute(f"UPDATE queue SET paused = 0 WHERE client_id = {ph}", (client_id,))
    conn.commit()
    cur.close()
    conn.close()


init_db()
