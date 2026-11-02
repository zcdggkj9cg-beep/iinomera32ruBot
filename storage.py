import os
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "")

def _connect():
    if DATABASE_URL:
        import psycopg
        return psycopg.connect(DATABASE_URL, autocommit=True)
    import sqlite3
    conn = sqlite3.connect("local-dev.sqlite3")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """ if DATABASE_URL else """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
    finally:
        conn.close()

def save_message(user_id: int, role: str, content: str):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (%s, %s, %s)"
            if DATABASE_URL else
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content[:4000]),
        )
    finally:
        conn.close()

def get_history(user_id: int, limit: int = 10):
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT role, content FROM messages WHERE user_id=%s ORDER BY id DESC LIMIT %s"
            if DATABASE_URL else
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return list(reversed(cur.fetchall()))
    finally:
        conn.close()
