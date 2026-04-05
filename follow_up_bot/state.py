"""
Lead state management using SQLite.
Tracks each lead through the follow-up sequence.
"""

import sqlite3
import pathlib
import datetime

DB_PATH = pathlib.Path(__file__).parent / "leads.db"

STATES = [
    "POST_CALL",       # Waiting to ask how call went
    "QUOTING",         # Pietro said it went well — quote due tomorrow
    "QUOTE_SENT",      # Quote sent — following up in 3 days
    "FOLLOWING_UP",    # Pietro is chasing
    "WON",             # Closed
    "LOST",            # Not interested / went elsewhere
    "WRITEOFF",        # Pietro gave up
]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL,
            contact_id TEXT,
            first_name TEXT,
            last_name TEXT,
            phone TEXT,
            email TEXT,
            business_name TEXT,
            suburb TEXT,
            service TEXT,
            call_time TEXT,
            state TEXT DEFAULT 'POST_CALL',
            waiting_reply INTEGER DEFAULT 0,
            next_action_at TEXT,
            last_updated TEXT,
            notes TEXT
        )
    """)
    conn.commit()
    conn.close()


def add_lead(client_id, contact_id, first_name, last_name, phone, email,
             business_name, suburb, service, call_time):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO leads
        (client_id, contact_id, first_name, last_name, phone, email,
         business_name, suburb, service, call_time, state, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'POST_CALL', ?)
    """, (client_id, contact_id, first_name, last_name, phone, email,
          business_name, suburb, service, call_time,
          datetime.datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def get_waiting_lead(client_id):
    """Get the one lead currently waiting on a reply from the client."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT * FROM leads
        WHERE client_id = ? AND waiting_reply = 1
        ORDER BY last_updated ASC LIMIT 1
    """, (client_id,)).fetchone()
    conn.close()
    if row:
        return dict(zip([col[0] for col in conn.execute("PRAGMA table_info(leads)").fetchall()], row))
    return None


def get_leads_due(client_id):
    """Get all leads where next_action_at is now or in the past."""
    conn = sqlite3.connect(DB_PATH)
    now = datetime.datetime.utcnow().isoformat()
    rows = conn.execute("""
        SELECT * FROM leads
        WHERE client_id = ? AND waiting_reply = 0
        AND next_action_at <= ? AND state NOT IN ('WON', 'LOST', 'WRITEOFF')
        ORDER BY next_action_at ASC
    """, (client_id, now)).fetchall()
    cols = [col[1] for col in conn.execute("PRAGMA table_info(leads)").fetchall()]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def update_lead(lead_id, **kwargs):
    kwargs["last_updated"] = datetime.datetime.utcnow().isoformat()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [lead_id]
    conn = sqlite3.connect(DB_PATH)
    conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", values)
    conn.commit()
    conn.close()


def get_lead_by_id(lead_id):
    conn = sqlite3.connect(DB_PATH)
    cols = [col[1] for col in conn.execute("PRAGMA table_info(leads)").fetchall()]
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    return dict(zip(cols, row)) if row else None


init_db()
