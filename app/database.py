"""SQLite database setup and helpers."""

import aiosqlite
import os

DB_PATH = os.getenv("DB_PATH", "openclaw_hosted.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    name TEXT,
    paddle_subscription_id TEXT UNIQUE,
    paddle_customer_id TEXT,
    plan TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS instances (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    hetzner_server_id INTEGER,
    server_ip TEXT,
    server_name TEXT,
    setup_password TEXT,
    status TEXT NOT NULL DEFAULT 'provisioning',
    health_status TEXT DEFAULT 'unknown',
    last_health_check TEXT,
    provision_log TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT REFERENCES instances(id),
    customer_id TEXT REFERENCES customers(id),
    event_type TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def get_db() -> aiosqlite.Connection:
    """Get a database connection. Caller must close it."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    """Initialize database schema."""
    db = await get_db()
    try:
        await db.executescript(SCHEMA)
        await db.commit()
    finally:
        await db.close()
