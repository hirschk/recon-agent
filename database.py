"""
database.py — SQLite schema, connection management, and audit log write guard.

IMPORTANT: The write_audit_log() function is the ONLY permitted path for
inserting into the audit_log table. No raw INSERT INTO audit_log anywhere
else in the codebase. This is enforced by convention + code review.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

DATABASE_PATH = os.environ.get("DATABASE_PATH", "recon.db")


# ─── Connection ──────────────────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ─── Schema Init ─────────────────────────────────────────────────────────────

def init_db() -> None:
    conn = get_connection()
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS gl_entries (
                id               TEXT PRIMARY KEY,
                date             TEXT NOT NULL,
                amount           REAL NOT NULL,
                account_code     TEXT NOT NULL,
                account_name     TEXT NOT NULL,
                description      TEXT,
                vendor           TEXT,
                reference_number TEXT,
                status           TEXT NOT NULL DEFAULT 'pending'
            );

            CREATE TABLE IF NOT EXISTS ap_subledger_entries (
                id               TEXT PRIMARY KEY,
                date             TEXT NOT NULL,
                amount           REAL NOT NULL,
                vendor           TEXT NOT NULL,
                gl_account       TEXT,
                invoice_number   TEXT,
                status           TEXT NOT NULL DEFAULT 'open',
                reference_number TEXT,
                invoice_url      TEXT
            );

            CREATE TABLE IF NOT EXISTS bank_transactions (
                id               TEXT PRIMARY KEY,
                date             TEXT NOT NULL,
                amount           REAL NOT NULL,
                description      TEXT,
                reference_number TEXT
            );

            CREATE TABLE IF NOT EXISTS match_results (
                id                   TEXT PRIMARY KEY,
                gl_entry_id          TEXT,
                subledger_entry_id   TEXT,
                bank_transaction_id  TEXT,
                match_status         TEXT NOT NULL,
                confidence_score     REAL,
                reasoning            TEXT,
                matched_at           TEXT NOT NULL,
                FOREIGN KEY (gl_entry_id)         REFERENCES gl_entries(id),
                FOREIGN KEY (subledger_entry_id)  REFERENCES ap_subledger_entries(id),
                FOREIGN KEY (bank_transaction_id) REFERENCES bank_transactions(id)
            );

            -- Immutable append-only audit log.
            -- Write via write_audit_log() ONLY. Never INSERT directly.
            CREATE TABLE IF NOT EXISTS audit_log (
                id           TEXT PRIMARY KEY,
                timestamp    TEXT NOT NULL,
                entity_type  TEXT NOT NULL,
                entity_id    TEXT NOT NULL,
                field_changed TEXT NOT NULL,
                old_value    TEXT,
                new_value    TEXT,
                actor        TEXT NOT NULL,
                reason       TEXT NOT NULL,
                action_type  TEXT NOT NULL,
                immutable    INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS reconciliation_sessions (
                id          TEXT PRIMARY KEY,
                period      TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                gl_file_name TEXT,
                summary     TEXT
            );
        """)
    conn.close()


# ─── Audit Log Write Guard ────────────────────────────────────────────────────
# This is the ONLY function in the codebase that may write to audit_log.
# Every amendment, approval, rejection, and agent action flows through here.

def write_audit_log(
    entity_type: str,
    entity_id: str,
    field_changed: str,
    old_value: Optional[str],
    new_value: Optional[str],
    actor: str,
    reason: str,
    action_type: str,
    timestamp: Optional[str] = None,
) -> str:
    """
    Append an immutable audit event. Returns the new log entry ID.

    Parameters
    ----------
    entity_type  : "GLEntry" | "APEntry" | "MatchResult" | "Session"
    entity_id    : The ID of the entity being acted upon (e.g. "GL-0023")
    field_changed: Which field changed (e.g. "amount", "status")
    old_value    : Previous value as a string (None if new entity)
    new_value    : New value as a string
    actor        : "system" | "agent" | "controller@acme.com" (Slack user email)
    reason       : Mandatory human-readable explanation — cannot be null
    action_type  : "amendment" | "approval" | "rejection" |
                   "duplicate_flagged" | "entry_posted" | "session_started"
    timestamp    : ISO-8601 UTC string; defaults to server time at call
    """
    if not reason or not reason.strip():
        raise ValueError("AuditLog.reason is mandatory and cannot be null or empty")
    if not actor or not actor.strip():
        raise ValueError("AuditLog.actor is mandatory and cannot be null or empty")

    log_id = f"AL-{uuid.uuid4().hex[:8].upper()}"
    ts = timestamp or datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO audit_log
                    (id, timestamp, entity_type, entity_id, field_changed,
                     old_value, new_value, actor, reason, action_type, immutable)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    log_id, ts, entity_type, entity_id, field_changed,
                    old_value, new_value, actor, reason, action_type,
                ),
            )
    finally:
        conn.close()

    return log_id


# ─── GL Entries ──────────────────────────────────────────────────────────────

def get_gl_entry(entry_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM gl_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_gl_entries() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM gl_entries ORDER BY date").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_gl_entry_status(entry_id: str, new_status: str) -> None:
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE gl_entries SET status = ? WHERE id = ?",
            (new_status, entry_id),
        )
    conn.close()


def load_gl_entries_from_df(df) -> None:
    """Bulk-insert GL entries from a pandas DataFrame (from CSV upload)."""
    conn = get_connection()
    with conn:
        for _, row in df.iterrows():
            conn.execute(
                """
                INSERT OR REPLACE INTO gl_entries
                    (id, date, amount, account_code, account_name,
                     description, vendor, reference_number, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row["id"]),
                    str(row["date"]),
                    float(row["amount"]),
                    str(row["account_code"]),
                    str(row["account_name"]),
                    str(row.get("description", "")),
                    str(row.get("vendor", "")),
                    str(row.get("reference_number", "")),
                    str(row.get("status", "pending")),
                ),
            )
    conn.close()


# ─── AP Subledger ─────────────────────────────────────────────────────────────

def get_all_ap_entries() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM ap_subledger_entries ORDER BY date"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_ap_entry(entry_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM ap_subledger_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def load_ap_entries_from_df(df) -> None:
    conn = get_connection()
    with conn:
        for _, row in df.iterrows():
            conn.execute(
                """
                INSERT OR REPLACE INTO ap_subledger_entries
                    (id, date, amount, vendor, gl_account,
                     invoice_number, status, reference_number, invoice_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row["id"]),
                    str(row["date"]),
                    float(row["amount"]),
                    str(row["vendor"]),
                    str(row.get("gl_account", "")),
                    str(row.get("invoice_number", "")),
                    str(row.get("status", "open")),
                    str(row.get("reference_number", "")),
                    str(row["invoice_url"]) if row.get("invoice_url") else None,
                ),
            )
    conn.close()


# ─── Bank Transactions ────────────────────────────────────────────────────────

def get_all_bank_transactions() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM bank_transactions ORDER BY date"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load_bank_transactions_from_df(df) -> None:
    conn = get_connection()
    with conn:
        for _, row in df.iterrows():
            conn.execute(
                """
                INSERT OR REPLACE INTO bank_transactions
                    (id, date, amount, description, reference_number)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(row["id"]),
                    str(row["date"]),
                    float(row["amount"]),
                    str(row.get("description", "")),
                    str(row.get("reference_number", "")),
                ),
            )
    conn.close()


# ─── Match Results ────────────────────────────────────────────────────────────

def save_match_result(result: dict) -> None:
    conn = get_connection()
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO match_results
                (id, gl_entry_id, subledger_entry_id, bank_transaction_id,
                 match_status, confidence_score, reasoning, matched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["id"],
                result.get("gl_entry_id"),
                result.get("subledger_entry_id"),
                result.get("bank_transaction_id"),
                result["match_status"],
                result.get("confidence_score"),
                result.get("reasoning"),
                result["matched_at"],
            ),
        )
    conn.close()


def get_all_match_results() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM match_results").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_match_result_for_gl(gl_entry_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM match_results WHERE gl_entry_id = ? ORDER BY matched_at DESC LIMIT 1",
            (gl_entry_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ─── Audit Log Reads ──────────────────────────────────────────────────────────

def get_audit_trail(entity_id: str) -> list[dict]:
    """Return full audit chain for an entity, oldest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY timestamp ASC",
            (entity_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_audit_logs(limit: int = 500) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ─── Reconciliation Sessions ──────────────────────────────────────────────────

def save_reconciliation_session(session: dict) -> None:
    conn = get_connection()
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO reconciliation_sessions
                (id, period, created_at, gl_file_name, summary)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session["id"],
                session["period"],
                session["created_at"],
                session.get("gl_file_name"),
                json.dumps(session.get("summary", {})),
            ),
        )
    conn.close()


def get_latest_session() -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM reconciliation_sessions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        s = dict(row)
        s["summary"] = json.loads(s["summary"] or "{}")
        return s
    finally:
        conn.close()
