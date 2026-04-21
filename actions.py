"""
actions.py — Human-in-the-loop button handlers.

Handles:
  approve_entry  — controller clicked "Yes, post it"
  reject_entry   — controller clicked "No, skip"

Both flows:
  1. Validate the incoming action payload
  2. Update GL entry status in the database
  3. Write an immutable audit log event
  4. Reply in the Slack thread with a confirmation

Slack user → email mapping:
  For demo purposes, all Slack users are mapped to controller@acme.com.
  In production, resolve via Slack Users API: client.users_info(user=user_id)
  and read the profile.email field (requires users:read.email OAuth scope).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import database as db


# ─── Slack user → email resolution ────────────────────────────────────────────

def resolve_actor(user_id: str, client=None) -> str:
    """
    Map a Slack user ID to an email address for the audit log actor field.

    Production path (commented out — requires users:read.email scope):
        response = client.users_info(user=user_id)
        return response["user"]["profile"].get("email", f"slack:{user_id}")

    Demo: hardcode to controller@acme.com.
    """
    # TODO: uncomment for production Slack identity resolution:
    # if client:
    #     try:
    #         response = client.users_info(user=user_id)
    #         return response["user"]["profile"].get("email", f"slack:{user_id}")
    #     except Exception:
    #         pass
    return "controller@acme.com"


# ─── Approval handler ─────────────────────────────────────────────────────────

def handle_approve(
    gl_entry_id: str,
    action_value: str,
    slack_user_id: str,
    say,
    client=None,
) -> None:
    """
    Controller clicked "Yes, post it".

    1. Look up the GL entry
    2. Update status: pending → posted
    3. Write approval to audit log (immutable)
    4. Post confirmation in thread
    """
    gl = db.get_gl_entry(gl_entry_id)
    if not gl:
        say(f"Error: GL entry `{gl_entry_id}` not found.")
        return

    actor = resolve_actor(slack_user_id, client)
    ts_now = datetime.now(timezone.utc).isoformat()

    old_status = gl["status"]

    # Update GL entry status
    db.update_gl_entry_status(gl_entry_id, "posted")

    # Write immutable audit log event
    log_id = db.write_audit_log(
        entity_type="GLEntry",
        entity_id=gl_entry_id,
        field_changed="status",
        old_value=old_status,
        new_value="posted",
        actor=actor,
        reason=f"Controller approved via Slack — {gl.get('description', gl_entry_id)}",
        action_type="approval",
        timestamp=ts_now,
    )

    # Format timestamp for display
    ts_display = datetime.fromisoformat(ts_now).strftime("%Y-%m-%dT%H:%M:%SZ")

    say(
        f"✓ Done. Adjusting entry posted to account {gl['account_code']}.\n"
        f"Logged: `{actor}` approved via Slack at `{ts_display}`\n"
        f"Audit ID: `{log_id}`"
    )


# ─── Rejection handler ────────────────────────────────────────────────────────

def handle_reject(
    gl_entry_id: str,
    action_value: str,
    slack_user_id: str,
    say,
    client=None,
) -> None:
    """
    Controller clicked "No, skip".

    1. Look up the GL entry
    2. Write rejection to audit log (immutable, status unchanged)
    3. Post confirmation in thread
    """
    gl = db.get_gl_entry(gl_entry_id)
    if not gl:
        say(f"Error: GL entry `{gl_entry_id}` not found.")
        return

    actor = resolve_actor(slack_user_id, client)
    ts_now = datetime.now(timezone.utc).isoformat()

    log_id = db.write_audit_log(
        entity_type="GLEntry",
        entity_id=gl_entry_id,
        field_changed="status",
        old_value=gl["status"],
        new_value="skipped",
        actor=actor,
        reason=f"Controller rejected via Slack — {gl.get('description', gl_entry_id)}",
        action_type="rejection",
        timestamp=ts_now,
    )

    say(
        "Understood. Entry skipped and logged. "
        "You can revisit this in the exceptions report.\n"
        f"Audit ID: `{log_id}`"
    )


# ─── Duplicate flag handler ───────────────────────────────────────────────────

def handle_flag_duplicate(
    gl_entry_id_a: str,
    gl_entry_id_b: str,
    slack_user_id: str,
    say,
    client=None,
) -> None:
    """
    Controller clicked "Yes, flag it" on a suspected duplicate.

    Flags both entries for AP review. Does not post either entry.
    """
    actor = resolve_actor(slack_user_id, client)
    ts_now = datetime.now(timezone.utc).isoformat()

    for entry_id in [gl_entry_id_a, gl_entry_id_b]:
        gl = db.get_gl_entry(entry_id)
        if not gl:
            continue
        db.update_gl_entry_status(entry_id, "duplicate")
        db.write_audit_log(
            entity_type="GLEntry",
            entity_id=entry_id,
            field_changed="status",
            old_value=gl["status"],
            new_value="duplicate",
            actor=actor,
            reason="Controller confirmed suspected duplicate — flagged for AP review via Slack",
            action_type="duplicate_flagged",
            timestamp=ts_now,
        )

    say(
        f"Flagged. Both entries ({gl_entry_id_a} and {gl_entry_id_b}) marked for AP review.\n"
        "They will appear in the exceptions report and will not be auto-posted."
    )


def handle_dismiss_duplicate(
    gl_entry_id_a: str,
    gl_entry_id_b: str,
    slack_user_id: str,
    say,
    client=None,
) -> None:
    """Controller clicked "No, these are separate" on a suspected duplicate."""
    actor = resolve_actor(slack_user_id, client)
    ts_now = datetime.now(timezone.utc).isoformat()

    for entry_id in [gl_entry_id_a, gl_entry_id_b]:
        gl = db.get_gl_entry(entry_id)
        if not gl:
            continue
        db.write_audit_log(
            entity_type="GLEntry",
            entity_id=entry_id,
            field_changed="status",
            old_value=gl["status"],
            new_value="pending",
            actor=actor,
            reason="Controller dismissed duplicate flag — entries confirmed as separate transactions",
            action_type="rejection",
            timestamp=ts_now,
        )

    say(
        "Got it. Entries confirmed as separate transactions.\n"
        "Both remain in the reconciliation for normal matching."
    )
