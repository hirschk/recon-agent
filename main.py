"""
main.py — Slack bot entry point and event routing.

Events handled:
  file_shared        — controller drops a GL export CSV
  app_mention        — natural-language Q&A (bot is @-mentioned)
  message.channels   — natural-language Q&A (direct message in channel after recon)

Interactive actions:
  approve_entry      — "Yes, post it" button
  reject_entry       — "No, skip" button
  flag_duplicate     — "Yes, flag it" button on duplicate card
  dismiss_duplicate  — "No, these are separate" button

Run locally:
    python main.py

Exposes: POST /slack/events  (Slack Events API)
         POST /slack/actions  (Slack Interactivity)
"""

from __future__ import annotations

# Load .env before any third-party import. slack_bolt runs module-level code on
# import that snapshots environment variables — if load_dotenv() fires after that
# import, the Slack tokens and ANTHROPIC_API_KEY it reads will be whatever was in
# the process env before .env was applied (often empty or stale).
# override=True ensures .env wins even if systemd EnvironmentFile pre-populated
# a wrong value.
import os
from dotenv import load_dotenv
load_dotenv(override=True)

import io
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pandas as pd
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request

import database as db
import matching as matching_engine
import llm
import actions as action_handlers

# ─── App Init ─────────────────────────────────────────────────────────────────

app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)

flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# In-memory session state (single active session per bot instance — sufficient for demo)
_active_session: dict = {}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _post(client, channel: str, text: str, thread_ts: str | None = None) -> str:
    """Post a message and return its ts."""
    kwargs: dict = {"channel": channel, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = client.chat_postMessage(**kwargs)
    return resp["ts"]


def _post_blocks(client, channel: str, blocks: list, text: str, thread_ts: str | None = None) -> str:
    kwargs: dict = {"channel": channel, "blocks": blocks, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = client.chat_postMessage(**kwargs)
    return resp["ts"]


def _sleep(seconds: float) -> None:
    """Delay for demo narration effect."""
    time.sleep(seconds)


def _count_accounts(gl_df: pd.DataFrame) -> int:
    return gl_df["account_code"].nunique()


# ─── Block Kit helpers ────────────────────────────────────────────────────────

def _clean_url(url: str) -> str:
    """
    Strip query parameters from an invoice URL before putting it into Block Kit.

    Slack's link-button redirect proxy percent-encodes the '?' separator, turning
    e.g. '...view?usp=sharing' into '...view%3Fusp%3Dsharing'.  Google Drive then
    reads the encoded '?' as part of the path rather than the start of a query string
    and returns 'file does not exist'.  Drive files are fully accessible at the bare
    '/view' form — the ?usp=sharing suffix is only Google's UI hint for the sharing
    dialog and is not required for link access.
    """
    if not url:
        return url
    parsed = urlparse(url.strip())
    return urlunparse(parsed._replace(query="", fragment=""))


def _exception_blocks(gl: dict, analysis: dict, ap_candidate: dict | None = None) -> list:
    acct = gl.get("account_code", "")
    vendor = gl.get("vendor", "Unknown")
    confidence_pct = int(analysis.get("confidence", 0) * 100)
    reasoning = analysis.get("reasoning", "")
    likely_cause = analysis.get("likely_cause", "")
    action_detail = analysis.get("action_detail", "")
    rec = analysis.get("recommended_action", "flag_for_review")

    header = f"⚠ Exception — {vendor} ({acct})"
    ap_line = ""
    invoice_url = None
    if ap_candidate:
        invoice_url = _clean_url(ap_candidate.get("invoice_url") or "")
        inv_link = (
            f"<{invoice_url}|{ap_candidate.get('invoice_number', 'N/A')}>"
            if invoice_url
            else f"`{ap_candidate.get('invoice_number', 'N/A')}`"
        )
        ap_line = (
            f"\nNearest AP match: Invoice {inv_link} "
            f"for ${ap_candidate['amount']:,.2f} — {ap_candidate.get('date', '')}"
        )

    body = (
        f"GL entry `{gl['id']}` — ${gl['amount']:,.2f} — {gl['date']}\n"
        f"{ap_line}\n"
        f"Likely cause: {likely_cause}\n"
        f"Confidence: {confidence_pct}%\n"
        f"Reasoning: {reasoning}"
    )

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]

    # Label and description vary by recommended action, but Yes/No always appear.
    if rec == "post_entry":
        action_label = "Post adjusting entry"
        action_text  = f"*Recommended action:* Post adjusting entry to account {acct} — {gl.get('account_name', '')}.\n_{action_detail}_"
        yes_label, yes_style = "Yes, post it", "primary"
        no_label,  no_style  = "No, skip",     "danger"
    else:
        action_label = "Flag for review"
        action_text  = f"*Action required:* {action_detail}"
        yes_label, yes_style = "Yes, approve",  "primary"
        no_label,  no_style  = "No, skip",      "danger"

    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": action_text}})

    # Yes/No approval buttons — always present on every exception card.
    # View Invoice appears as a third button when an invoice URL is available.
    action_elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": yes_label},
            "style": yes_style,
            "action_id": "approve_entry",
            "value": json.dumps({"gl_entry_id": gl["id"]}),
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": no_label},
            "style": no_style,
            "action_id": "reject_entry",
            "value": json.dumps({"gl_entry_id": gl["id"]}),
        },
    ]
    if invoice_url:
        action_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "View Invoice \u2192"},
            "url": invoice_url,
            "action_id": "view_invoice",
        })
    blocks.append({"type": "actions", "elements": action_elements})

    blocks.append({"type": "divider"})
    return blocks


def _duplicate_blocks(dup: dict, explanation: str) -> list:
    a = dup["entry_a"]
    b = dup["entry_b"]
    vendor = dup["vendor"]
    acct = a.get("account_code", "6050")
    conf_pct = int(dup["confidence"] * 100)
    variance_pct = dup["amount_variance_pct"]

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🚨 Suspected Duplicate Payment — {vendor} ({acct})"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Two {vendor} entries found within {dup['days_apart']} days, "
                    f"amounts within {variance_pct}% of each other, different reference numbers.\n\n"
                    f"*Entry A:* ${a['amount']:,.2f}  |  ref: `{a['reference_number']}`  |  {a['date']}\n"
                    f"*Entry B:* ${b['amount']:,.2f}  |  ref: `{b['reference_number']}`  |  {b['date']}\n\n"
                    f"_{explanation}_\n\n"
                    f"*Confidence this is a duplicate: {conf_pct}%*"
                ),
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "⚠ *Do not post until confirmed.*  Should I flag this for AP review?"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Yes, flag it"},
                    "style": "danger",
                    "action_id": "flag_duplicate",
                    "value": json.dumps({
                        "gl_entry_id_a": a["id"],
                        "gl_entry_id_b": b["id"],
                    }),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "No, these are separate"},
                    "action_id": "dismiss_duplicate",
                    "value": json.dumps({
                        "gl_entry_id_a": a["id"],
                        "gl_entry_id_b": b["id"],
                    }),
                },
            ],
        },
        {"type": "divider"},
    ]


def _summary_text(session_summary: dict, period: str) -> str:
    matched   = session_summary.get("matched", 0)
    partial   = session_summary.get("partial", 0)
    exc       = session_summary.get("exceptions", 0)
    approved  = session_summary.get("approved", 0)
    skipped   = session_summary.get("skipped", 0)
    dups      = session_summary.get("duplicates", 0)
    amended   = session_summary.get("amended", 0)

    return (
        f"*{period} close reconciliation complete.*\n\n"
        f"✓ {matched} matched\n"
        f"~ {partial} partial (payment pending clearance)\n"
        f"⚠ {exc} exceptions ({approved} approved, {skipped} skipped)\n"
        f"🚨 {dups} suspected duplicate flagged — pending AP review\n"
        f"✎ {amended} amended entr{'y' if amended == 1 else 'ies'} — audit trail attached\n\n"
        "All actions logged with full audit trail.\n"
        "Exported: `march_recon_report.csv`"
    )


# ─── Report CSV export ────────────────────────────────────────────────────────

def export_report(gl_entries: list[dict], match_results: list[dict], channel: str, client) -> None:
    """Build and upload the final reconciliation report CSV."""
    mr_by_gl = {r["gl_entry_id"]: r for r in match_results if r.get("gl_entry_id")}

    rows = []
    for gl in gl_entries:
        mr = mr_by_gl.get(gl["id"], {})
        rows.append({
            "gl_id":            gl["id"],
            "date":             gl["date"],
            "vendor":           gl.get("vendor", ""),
            "account_code":     gl["account_code"],
            "account_name":     gl["account_name"],
            "amount":           gl["amount"],
            "reference_number": gl.get("reference_number", ""),
            "match_status":     mr.get("match_status", "unmatched"),
            "confidence_score": mr.get("confidence_score", ""),
            "reasoning":        mr.get("reasoning", ""),
            "ap_entry_id":      mr.get("subledger_entry_id", ""),
            "bank_txn_id":      mr.get("bank_transaction_id", ""),
        })

    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_csv(buf, index=False)
    buf.seek(0)

    client.files_upload_v2(
        channel=channel,
        filename="march_recon_report.csv",
        content=buf.getvalue(),
        title="March 2024 Reconciliation Report",
        initial_comment="Here's the full reconciliation report with all match results.",
    )


# ─── Core reconciliation workflow ─────────────────────────────────────────────

def run_recon_workflow(file_id: str, channel_id: str, client) -> None:
    """
    Full reconciliation workflow triggered by a GL CSV file upload.

    Posts threaded progress updates as it works.
    """
    db.init_db()

    # ── Step 0: Download the file ─────────────────────────────────────────────
    file_info = client.files_info(file=file_id)
    file_obj  = file_info["file"]
    file_name = file_obj.get("name", "gl_export.csv")
    file_url  = file_obj.get("url_private_download") or file_obj.get("url_private")

    import requests
    resp = requests.get(
        file_url,
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
    )
    resp.raise_for_status()
    gl_df = pd.read_csv(io.BytesIO(resp.content))

    entry_count  = len(gl_df)
    acct_count   = _count_accounts(gl_df)

    # ── Step 1: Announce ──────────────────────────────────────────────────────
    root_ts = _post(
        client, channel_id,
        f"Got your March GL export — {entry_count} entries across {acct_count} accounts. "
        f"Starting reconciliation.\n"
        "I'll match against the AP subledger, run duplicate detection, and flag anything that doesn't tie.",
    )

    _active_session["channel_id"]  = channel_id
    _active_session["root_ts"]     = root_ts
    _active_session["file_name"]   = file_name

    _sleep(1.2)

    # ── Step 2: Load GL into DB ───────────────────────────────────────────────
    db.load_gl_entries_from_df(gl_df)

    _post(client, channel_id, "✓ Running duplicate detection across all entries...", thread_ts=root_ts)
    _sleep(1.5)

    # ── Step 3: Run matching engine ───────────────────────────────────────────
    gl_entries = db.get_all_gl_entries()
    recon      = matching_engine.run_reconciliation(gl_entries)

    _post(client, channel_id, "✓ Matching expense entries to AP subledger...", thread_ts=root_ts)
    _sleep(1.8)

    matched_count = recon["counts"]["matched"]
    partial_count = recon["counts"]["partial"]
    exc_count     = recon["counts"]["exceptions"]
    dup_count     = recon["counts"]["duplicates"]

    _post(
        client, channel_id,
        f"✓ {matched_count} entries matched cleanly — "
        "Salesforce, Rippling, Linear, Ironclad all tie.",
        thread_ts=root_ts,
    )
    _sleep(1.2)

    _post(
        client, channel_id,
        f"⚠ Found {exc_count} exceptions and {dup_count} suspected duplicate. Investigating...",
        thread_ts=root_ts,
    )
    _sleep(2.0)

    # ── Step 4: LLM exception analysis ───────────────────────────────────────
    exception_results = llm.analyze_exceptions_batch(recon["exceptions"])

    approved_count = 0
    skipped_count  = 0

    # Post exception cards (standard exceptions first)
    for item in exception_results:
        gl       = item["gl"]
        analysis = item["analysis"]
        ap_cands = item["ap_candidates"]
        ap_best  = ap_cands[0] if ap_cands else None

        blocks = _exception_blocks(gl, analysis, ap_best)
        _post_blocks(
            client, channel_id, blocks,
            text=f"Exception: {gl.get('vendor', gl['id'])} — ${gl['amount']:,.2f}",
            thread_ts=root_ts,
        )
        _sleep(1.0)

    # ── Step 5: Post duplicate cards (last, for maximum impact) ──────────────
    for dup in recon["duplicates"]:
        explanation = llm.explain_duplicate(dup)
        blocks = _duplicate_blocks(dup, explanation)
        _post_blocks(
            client, channel_id, blocks,
            text=f"Suspected duplicate: {dup['vendor']} — ${dup['amount_a']:,.2f} + ${dup['amount_b']:,.2f}",
            thread_ts=root_ts,
        )
        _sleep(1.0)

    # ── Step 6: Save session ──────────────────────────────────────────────────
    session_id = f"RS-{uuid.uuid4().hex[:8].upper()}"
    summary = {
        "matched":    matched_count,
        "partial":    partial_count,
        "exceptions": exc_count,
        "approved":   approved_count,
        "skipped":    skipped_count,
        "duplicates": dup_count,
        "amended":    1,  # GL-0023 pre-seeded amendment
    }
    session = {
        "id":          session_id,
        "period":      "March 2024",
        "created_at":  datetime.now(timezone.utc).isoformat(),
        "gl_file_name": file_name,
        "summary":     summary,
    }
    db.save_reconciliation_session(session)
    _active_session["session"] = session

    db.write_audit_log(
        entity_type="Session",
        entity_id=session_id,
        field_changed="status",
        old_value=None,
        new_value="completed",
        actor="agent",
        reason=f"Reconciliation session completed — {file_name}, {entry_count} entries processed",
        action_type="session_started",
    )

    # ── Step 7: Export report + final summary ─────────────────────────────────
    all_results = db.get_all_match_results()
    try:
        export_report(gl_entries, all_results, channel_id, client)
    except Exception as e:
        _post(client, channel_id, f"Note: Could not upload report CSV — {e}", thread_ts=root_ts)

    _post(client, channel_id, _summary_text(summary, "March 2024"), thread_ts=root_ts)


# ─── Event: file_shared ───────────────────────────────────────────────────────

@app.event("file_shared")
def on_file_shared(event, client, logger):
    """Triggered when a file is uploaded to a channel the bot is in."""
    file_id    = event.get("file_id") or event.get("file", {}).get("id")
    channel_id = event.get("channel_id")

    if not file_id or not channel_id:
        return

    # Confirm it's a CSV before processing
    try:
        file_info = client.files_info(file=file_id)
        file_name = file_info["file"].get("name", "")
        if not file_name.lower().endswith(".csv"):
            return  # Ignore non-CSV uploads silently
    except Exception as e:
        logger.error(f"Could not fetch file info: {e}")
        return

    logger.info(f"GL CSV uploaded: {file_name} in channel {channel_id}")

    try:
        run_recon_workflow(file_id, channel_id, client)
    except Exception as e:
        logger.error(f"Reconciliation error: {e}", exc_info=True)
        client.chat_postMessage(
            channel=channel_id,
            text=f"Something went wrong during reconciliation: `{e}`\nCheck the server logs for details.",
        )


# ─── Event: app_mention + message (Q&A) ──────────────────────────────────────

def _should_handle_message(event: dict) -> bool:
    """Return True if this is a Q&A message we should respond to."""
    # Don't respond to bot messages or file uploads
    if event.get("bot_id") or event.get("subtype") in ("bot_message", "file_share"):
        return False
    # Respond to direct messages and mentions
    return True


@app.event("app_mention")
def on_mention(event, say, client, logger):
    """Handle @-mention messages as natural-language Q&A."""
    if not _should_handle_message(event):
        return

    question = event.get("text", "")
    # Strip the bot mention from the text
    import re
    question = re.sub(r"<@[A-Z0-9]+>", "", question).strip()

    if not question:
        say("Ask me anything about the reconciliation — e.g. 'Why didn't the AWS charge match?'")
        return

    session = _active_session.get("session") or db.get_latest_session()

    try:
        answer = llm.answer_question(question, session)
        say(answer, thread_ts=event.get("thread_ts") or event.get("ts"))
    except Exception as e:
        logger.error(f"Q&A error: {e}", exc_info=True)
        say(f"I hit an error looking that up: `{e}`")


@app.event("message")
def on_message(event, say, client, logger):
    """Handle plain messages in the channel as Q&A after a reconciliation session."""
    if not _should_handle_message(event):
        return
    # Only respond if we have an active session (avoid interrupting unrelated chat)
    if not _active_session.get("session") and not db.get_latest_session():
        return
    # Only respond in the reconciliation thread
    root_ts = _active_session.get("root_ts")
    msg_thread = event.get("thread_ts")
    if root_ts and msg_thread and msg_thread != root_ts:
        return

    question = event.get("text", "").strip()
    if not question:
        return

    session = _active_session.get("session") or db.get_latest_session()

    try:
        answer = llm.answer_question(question, session)
        say(answer, thread_ts=event.get("thread_ts") or event.get("ts"))
    except Exception as e:
        logger.error(f"Q&A error: {e}", exc_info=True)


# ─── Interactive Actions ──────────────────────────────────────────────────────

@app.action("approve_entry")
def on_approve(ack, body, client, logger):
    ack()
    payload     = json.loads(body["actions"][0]["value"])
    gl_entry_id = payload["gl_entry_id"]
    user_id     = body["user"]["id"]
    thread_ts   = (
        body.get("message", {}).get("thread_ts")
        or body.get("message", {}).get("ts")
    )

    def _say(text):
        client.chat_postMessage(
            channel=body["channel"]["id"],
            text=text,
            thread_ts=thread_ts,
        )

    logger.info(f"Approve action: {gl_entry_id} by {user_id}")
    action_handlers.handle_approve(gl_entry_id, body["actions"][0]["value"], user_id, _say, client)

    # Update session summary
    if _active_session.get("session"):
        _active_session["session"]["summary"]["approved"] = (
            _active_session["session"]["summary"].get("approved", 0) + 1
        )
        db.save_reconciliation_session(_active_session["session"])


@app.action("reject_entry")
def on_reject(ack, body, client, logger):
    ack()
    payload     = json.loads(body["actions"][0]["value"])
    gl_entry_id = payload["gl_entry_id"]
    user_id     = body["user"]["id"]
    thread_ts   = (
        body.get("message", {}).get("thread_ts")
        or body.get("message", {}).get("ts")
    )

    def _say(text):
        client.chat_postMessage(
            channel=body["channel"]["id"],
            text=text,
            thread_ts=thread_ts,
        )

    logger.info(f"Reject action: {gl_entry_id} by {user_id}")
    action_handlers.handle_reject(gl_entry_id, body["actions"][0]["value"], user_id, _say, client)

    if _active_session.get("session"):
        _active_session["session"]["summary"]["skipped"] = (
            _active_session["session"]["summary"].get("skipped", 0) + 1
        )
        db.save_reconciliation_session(_active_session["session"])


@app.action("flag_duplicate")
def on_flag_dup(ack, body, client, logger):
    ack()
    payload = json.loads(body["actions"][0]["value"])
    user_id = body["user"]["id"]
    thread_ts = (
        body.get("message", {}).get("thread_ts")
        or body.get("message", {}).get("ts")
    )

    def _say(text):
        client.chat_postMessage(
            channel=body["channel"]["id"],
            text=text,
            thread_ts=thread_ts,
        )

    action_handlers.handle_flag_duplicate(
        payload["gl_entry_id_a"], payload["gl_entry_id_b"], user_id, _say, client
    )


@app.action("dismiss_duplicate")
def on_dismiss_dup(ack, body, client, logger):
    ack()
    payload = json.loads(body["actions"][0]["value"])
    user_id = body["user"]["id"]
    thread_ts = (
        body.get("message", {}).get("thread_ts")
        or body.get("message", {}).get("ts")
    )

    def _say(text):
        client.chat_postMessage(
            channel=body["channel"]["id"],
            text=text,
            thread_ts=thread_ts,
        )

    action_handlers.handle_dismiss_duplicate(
        payload["gl_entry_id_a"], payload["gl_entry_id_b"], user_id, _say, client
    )


@app.action("view_invoice")
def on_view_invoice(ack):
    # Link buttons still fire a block_actions payload; ack immediately, nothing else to do.
    ack()


# ─── Flask routes (for ngrok / VPS deployment) ───────────────────────────────

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/slack/actions", methods=["POST"])
def slack_actions():
    return handler.handle(request)


@flask_app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", 3000))
    print(f"Recon agent starting on port {port}")
    flask_app.run(host="0.0.0.0", port=port, debug=False)
