"""
Database layer for SCIROOFING — backed by PostgreSQL via SQLAlchemy Core.

Reads DATABASE_URL from the environment (set by Railway).  When the variable is
absent the module exposes a no-op façade so the app still works in-memory for
local development without Postgres.
"""

import json
import logging
import os

from sqlalchemy import (
    create_engine, MetaData, Table, Column, Integer, Text, text,
)
from sqlalchemy.dialects.postgresql import JSONB

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# SQLAlchemy engine (created lazily in init_db)
engine = None
meta = MetaData()

# ── Table definitions ────────────────────────────────────────────────────────

users_table = Table(
    "users", meta,
    Column("username", Text, primary_key=True),
    Column("password", Text, nullable=False),
    Column("role", Text, nullable=False, server_default="client"),
    Column("brand", Text, nullable=False, server_default="generic"),
    Column("sender_email", Text, server_default=""),
)

email_lists_table = Table(
    "email_lists", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("client_username", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("emails", JSONB, server_default="[]"),
    Column("row_data", JSONB, server_default="{}"),
    Column("columns", JSONB, server_default="[]"),
    Column("uploaded_at", Text),
)

email_blasts_table = Table(
    "email_blasts", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("subject", Text),
    Column("body", Text),
    Column("from_name", Text),
    Column("sender_email", Text),
    Column("list_name", Text),
    Column("recipients", JSONB, server_default="[]"),
    Column("row_data", JSONB, server_default="{}"),
    Column("recipient_count", Integer, server_default="0"),
    Column("scheduled_for", Text),
    Column("status", Text, server_default="pending"),
    Column("sent_at", Text),
    Column("send_result", Text),
)

custom_spots_table = Table(
    "custom_spots", meta,
    Column("spot_id", Text, primary_key=True),
    Column("name", Text, server_default=""),
    Column("type", Text),
    Column("address", Text),
    Column("city", Text),
    Column("status", Text, server_default="New"),
    Column("coords", JSONB),
)

property_notes_table = Table(
    "property_notes", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("property_id", Integer, nullable=False),
    Column("brand", Text, nullable=False, server_default="generic"),
    Column("content", Text),
    Column("timestamp", Text),
)


# ── Initialisation ───────────────────────────────────────────────────────────

def is_enabled():
    return bool(DATABASE_URL)


def init_db():
    """Create engine + tables.  Call once at startup."""
    global engine
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set – running without Postgres (in-memory only).")
        return False

    url = DATABASE_URL
    # Railway may provide postgres:// which SQLAlchemy 2.x rejects; fix it.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
    meta.create_all(engine)
    logger.info("Postgres connected and tables ensured.")
    return True


# ── Users CRUD ───────────────────────────────────────────────────────────────

DEFAULT_USERS = {
    "admin":      {"password": "admin123",   "role": "admin",  "brand": "generic",    "sender_email": ""},
    "adminchan":  {"password": "icecream2",  "role": "admin",  "brand": "adminchan",  "sender_email": ""},
    "sci":        {"password": "sci123",     "role": "client", "brand": "sci",        "sender_email": "Shawn@sciroof.com"},
    "roofing123": {"password": "roofing123", "role": "client", "brand": "generic",    "sender_email": ""},
    "munsie":     {"password": "munsie123",  "role": "client", "brand": "munsie",     "sender_email": ""},
    "jobsdirect": {"password": "icecream2",  "role": "client", "brand": "jobsdirect", "sender_email": "choffman@becastaffing.com"},
}


def load_users():
    """Return users dict from DB.  Seeds defaults if table is empty."""
    if not engine:
        return dict(DEFAULT_USERS)

    with engine.connect() as conn:
        rows = conn.execute(users_table.select()).fetchall()
        if not rows:
            # Seed
            for uname, info in DEFAULT_USERS.items():
                conn.execute(users_table.insert().values(username=uname, **info))
            conn.commit()
            return dict(DEFAULT_USERS)
        return {r.username: {"password": r.password, "role": r.role, "brand": r.brand, "sender_email": r.sender_email or ""} for r in rows}


def save_user(username, info):
    if not engine:
        return
    with engine.connect() as conn:
        existing = conn.execute(users_table.select().where(users_table.c.username == username)).fetchone()
        if existing:
            conn.execute(users_table.update().where(users_table.c.username == username).values(**info))
        else:
            conn.execute(users_table.insert().values(username=username, **info))
        conn.commit()


def delete_user(username):
    if not engine:
        return
    with engine.connect() as conn:
        conn.execute(users_table.delete().where(users_table.c.username == username))
        conn.commit()


# ── Email lists CRUD ─────────────────────────────────────────────────────────

def load_email_manager_data():
    """Return EMAIL_MANAGER_DATA dict from DB."""
    if not engine:
        return {}
    result = {}
    with engine.connect() as conn:
        rows = conn.execute(email_lists_table.select()).fetchall()
        for r in rows:
            uname = r.client_username
            if uname not in result:
                result[uname] = {"lists": [], "schedules": []}
            result[uname]["lists"].append({
                "db_id": r.id,
                "name": r.name,
                "emails": r.emails or [],
                "row_data": r.row_data or {},
                "columns": r.columns or [],
                "uploaded_at": r.uploaded_at or "",
            })
    return result


def save_email_list(client_username, list_data):
    """Insert a new email list and return the DB id."""
    if not engine:
        return None
    with engine.connect() as conn:
        result = conn.execute(email_lists_table.insert().values(
            client_username=client_username,
            name=list_data["name"],
            emails=list_data.get("emails", []),
            row_data=list_data.get("row_data", {}),
            columns=list_data.get("columns", []),
            uploaded_at=list_data.get("uploaded_at", ""),
        ))
        conn.commit()
        return result.inserted_primary_key[0]


# ── Email blasts CRUD ────────────────────────────────────────────────────────

def load_email_blasts():
    """Return list of blast dicts from DB, newest first."""
    if not engine:
        return []
    with engine.connect() as conn:
        rows = conn.execute(email_blasts_table.select().order_by(email_blasts_table.c.id.desc())).fetchall()
        return [
            {
                "id": r.id,
                "subject": r.subject,
                "body": r.body,
                "from_name": r.from_name,
                "sender_email": r.sender_email,
                "list_name": r.list_name,
                "recipients": r.recipients or [],
                "row_data": r.row_data or {},
                "recipient_count": r.recipient_count or 0,
                "scheduled_for": r.scheduled_for,
                "status": r.status or "pending",
                "sent_at": r.sent_at,
                "send_result": r.send_result,
            }
            for r in rows
        ]


def save_blast(blast_dict):
    """Insert a new blast and return the DB id."""
    if not engine:
        return None
    data = {k: v for k, v in blast_dict.items() if k != "id"}
    with engine.connect() as conn:
        result = conn.execute(email_blasts_table.insert().values(**data))
        conn.commit()
        return result.inserted_primary_key[0]


def update_blast(blast_id, updates):
    """Update fields on an existing blast."""
    if not engine:
        return
    with engine.connect() as conn:
        conn.execute(email_blasts_table.update().where(email_blasts_table.c.id == blast_id).values(**updates))
        conn.commit()


# ── Custom spots CRUD ────────────────────────────────────────────────────────

def load_custom_spots():
    """Return list of custom spot dicts from DB."""
    if not engine:
        return None  # signals caller to fall back to JSON file
    with engine.connect() as conn:
        rows = conn.execute(custom_spots_table.select()).fetchall()
        return [
            {
                "id": r.spot_id,
                "name": r.name or "",
                "type": r.type,
                "address": r.address,
                "city": r.city,
                "status": r.status or "New",
                "coords": r.coords,
            }
            for r in rows
        ]


def save_custom_spot(spot):
    if not engine:
        return
    with engine.connect() as conn:
        conn.execute(custom_spots_table.insert().values(
            spot_id=spot["id"],
            name=spot.get("name", ""),
            type=spot.get("type"),
            address=spot.get("address"),
            city=spot.get("city"),
            status=spot.get("status", "New"),
            coords=spot.get("coords"),
        ))
        conn.commit()


def delete_custom_spot(spot_id):
    if not engine:
        return
    with engine.connect() as conn:
        conn.execute(custom_spots_table.delete().where(custom_spots_table.c.spot_id == spot_id))
        conn.commit()


# ── Property notes CRUD ──────────────────────────────────────────────────────

def load_property_notes(brand="generic"):
    """Return dict of {property_id: [note_dicts]} from DB."""
    if not engine:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            property_notes_table.select().where(property_notes_table.c.brand == brand)
        ).fetchall()
        result = {}
        for r in rows:
            result.setdefault(r.property_id, []).append({
                "content": r.content,
                "timestamp": r.timestamp,
            })
        return result


def save_property_note(property_id, brand, content, timestamp):
    if not engine:
        return
    with engine.connect() as conn:
        conn.execute(property_notes_table.insert().values(
            property_id=property_id,
            brand=brand,
            content=content,
            timestamp=timestamp,
        ))
        conn.commit()
