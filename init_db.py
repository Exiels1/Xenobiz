# init_db.py
import sqlite3

conn = sqlite3.connect("chat.db")
c = conn.cursor()

# Drop old tables if any
c.execute("DROP TABLE IF EXISTS messages")
c.execute("DROP TABLE IF EXISTS conversations")
c.execute("DROP TABLE IF EXISTS issues")
c.execute("DROP TABLE IF EXISTS resolution_flags")
c.execute("DROP TABLE IF EXISTS issue_events")
c.execute("DROP TABLE IF EXISTS handoff_locks")

# New table for conversation history
c.execute("""
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

c.execute("""
CREATE TABLE issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    last_reported TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

c.execute("""
CREATE TABLE resolution_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL UNIQUE,
    response_mode TEXT DEFAULT "NORMAL",
    disable_speculation INTEGER DEFAULT 0,
    simplify_output INTEGER DEFAULT 0,
    require_verifiable_only INTEGER DEFAULT 0,
    limit_scope INTEGER DEFAULT 0,
    refuse_if_repeated INTEGER DEFAULT 0,
    last_updated TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

c.execute("""
CREATE TABLE issue_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL,
    reported_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

c.execute("""
CREATE TABLE handoff_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL UNIQUE,
    locked INTEGER DEFAULT 0,
    last_updated TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

conn.commit()
conn.close()

print("✅ Database reset complete. conversations table is ready.")
