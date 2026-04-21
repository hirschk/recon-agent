"""
llm.py — Claude integration for exception reasoning and natural-language Q&A.

Called ONLY for GL entries that didn't get a clean deterministic match.
Clean matches never touch this module — keeps the run fast and cheap.

Model: claude-sonnet-4-6
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Optional

import anthropic

import database as db

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


MODEL = "claude-sonnet-4-6"

# ─── Exception Analysis ───────────────────────────────────────────────────────

EXCEPTION_SYSTEM = """You are a senior financial controller AI assistant specializing in
month-end close reconciliation for SaaS companies.

Your job: analyze a GL entry that failed deterministic matching and determine the most
likely explanation. Be concise, specific, and actionable.

Always respond with valid JSON in exactly this shape:
{
  "confidence":          0.0–1.0,
  "reasoning":           "One clear sentence explaining the mismatch.",
  "likely_cause":        "Brief label: e.g. 'Amount mismatch — credit applied'",
  "recommended_action":  "post_entry | flag_for_review | request_info | mark_unmatched",
  "action_detail":       "Specific instruction for the controller."
}

Confidence guide:
  0.9–1.0  Very likely match with explainable difference
  0.7–0.89 Probable match, needs controller confirmation
  0.5–0.69 Possible match, significant uncertainty
  <0.5     Unclear — flag for manual review

Never fabricate vendor names, amounts, or reference numbers not present in the context."""

EXCEPTION_USER_TMPL = """GL Entry to analyze:
{gl_json}

Candidate AP subledger entries (within 10% amount variance, 7-day window):
{candidates_json}

Existing audit log for this entity (if any):
{audit_json}

Additional context:
- Period: March 2024
- Company: SaaS startup, ~$2M ARR
- Chart of accounts: 6010 Software & SaaS | 6020 Cloud Infra | 6030 Contractor |
  6040 Marketing | 6050 Payroll | 6060 Legal | 6070 Travel

Analyze this entry and return your JSON response."""


def analyze_exception(
    gl: dict,
    ap_candidates: Optional[list[dict]] = None,
) -> dict:
    """
    Call Claude to analyze one GL exception entry.

    Parameters
    ----------
    gl            : GL entry dict (from database)
    ap_candidates : AP entries near this GL entry in amount/date/vendor space

    Returns
    -------
    dict with keys: confidence, reasoning, likely_cause, recommended_action, action_detail
    """
    audit_trail = db.get_audit_trail(gl["id"])

    if ap_candidates is None:
        # Find AP candidates: within 10% amount and 7 days
        all_ap = db.get_all_ap_entries()
        ap_candidates = []
        for ap in all_ap:
            try:
                amt_variance = abs(ap["amount"] - gl["amount"]) / max(abs(gl["amount"]), 0.01)
                if amt_variance > 0.10:
                    continue
                from matching import _within_days
                if not _within_days(gl["date"], ap["date"], 7):
                    continue
                ap_candidates.append(ap)
            except Exception:
                continue

    prompt = EXCEPTION_USER_TMPL.format(
        gl_json=json.dumps(gl, indent=2),
        candidates_json=json.dumps(ap_candidates, indent=2) if ap_candidates else "[]",
        audit_json=json.dumps(audit_trail, indent=2) if audit_trail else "[]",
    )

    client = _get_client()
    message = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=EXCEPTION_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {
            "confidence": 0.4,
            "reasoning": "Could not parse LLM response — manual review required.",
            "likely_cause": "Parse error",
            "recommended_action": "flag_for_review",
            "action_detail": "Review manually. Raw response: " + raw[:200],
        }

    return result


def analyze_exceptions_batch(
    exception_gl_entries: list[dict],
) -> list[dict]:
    """
    Analyze all exception GL entries. Returns list of enriched result dicts.
    Saves updated match results to the database.
    """
    enriched = []
    all_ap = db.get_all_ap_entries()

    for gl in exception_gl_entries:
        # Find AP candidates
        candidates = []
        for ap in all_ap:
            try:
                amt_variance = abs(ap["amount"] - gl["amount"]) / max(abs(gl["amount"]), 0.01)
                if amt_variance > 0.10:
                    continue
                from matching import _within_days
                if not _within_days(gl["date"], ap["date"], 7):
                    continue
                candidates.append(ap)
            except Exception:
                continue

        analysis = analyze_exception(gl, candidates)

        # Determine match status from recommended action
        status_map = {
            "post_entry":       "pending_approval",
            "flag_for_review":  "exception",
            "request_info":     "exception",
            "mark_unmatched":   "unmatched",
        }
        match_status = status_map.get(analysis.get("recommended_action", ""), "exception")

        # Find best AP candidate (first one, if any)
        best_ap_id = candidates[0]["id"] if candidates else None

        mr = {
            "id":                  f"MR-{uuid.uuid4().hex[:8].upper()}",
            "gl_entry_id":         gl["id"],
            "subledger_entry_id":  best_ap_id,
            "bank_transaction_id": None,
            "match_status":        match_status,
            "confidence_score":    analysis.get("confidence", 0.5),
            "reasoning":           analysis.get("reasoning", ""),
            "matched_at":          datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        db.save_match_result(mr)

        enriched.append({
            "gl": gl,
            "ap_candidates": candidates,
            "analysis": analysis,
            "match_result": mr,
        })

    return enriched


# ─── Natural Language Q&A ─────────────────────────────────────────────────────

QA_SYSTEM = """You are a financial controller AI assistant with full access to this
month's reconciliation data. Answer the controller's question clearly and concisely.

Rules:
- Cite specific GL IDs, amounts, and dates from the context provided
- When discussing an amended entry, walk through the full audit chain in plain English
- When discussing an approved entry, show who approved it and when
- Never say "I don't know" if the data is in the context — find it and explain it
- Keep answers under 400 words unless detail is specifically requested
- Do not use JSON in your response — plain English with bullet points or short paragraphs"""

QA_USER_TMPL = """Controller question: {question}

Active reconciliation session:
{session_json}

All match results from this session:
{match_results_json}

Relevant audit log entries (all entities mentioned in the question, or all if question is general):
{audit_json}

GL entries referenced:
{gl_entries_json}

Answer the controller's question using only the data above."""


def answer_question(
    question: str,
    session: Optional[dict] = None,
) -> str:
    """
    Answer a natural-language question from the controller about the reconciliation.

    Pulls full context from the database: session summary, match results,
    audit trail, and referenced GL entries.
    """
    if session is None:
        session = db.get_latest_session() or {}

    # Gather match results and GL entries
    match_results = db.get_all_match_results()
    gl_entries = db.get_all_gl_entries()

    # Build audit log — try to find referenced entity IDs from the question
    audit_entries = []
    # Pull audit entries for any GL IDs mentioned in question
    q_upper = question.upper()
    for gl in gl_entries:
        if gl["id"] in q_upper or (gl.get("vendor") and gl["vendor"].upper() in q_upper):
            audit_entries.extend(db.get_audit_trail(gl["id"]))
    # If no specific entries found, pull all recent audit entries
    if not audit_entries:
        audit_entries = db.get_all_audit_logs(limit=50)

    # Keep GL entries relevant to question (match results + referenced vendors)
    relevant_gl = []
    mentioned_vendors = []
    for gl in gl_entries:
        vendor = str(gl.get("vendor", "")).upper()
        if any(word in q_upper for word in ["AWS", "AMAZON"]) and "AMAZON" in vendor:
            relevant_gl.append(gl)
            mentioned_vendors.append(vendor)
        elif gl.get("vendor") and gl["vendor"].upper() in q_upper:
            relevant_gl.append(gl)
    if not relevant_gl:
        # Include all exception/amended/pending entries as context
        relevant_gl = [g for g in gl_entries if g.get("status") in (
            "exception", "amended", "pending_approval", "posted", "duplicate"
        )]

    prompt = QA_USER_TMPL.format(
        question=question,
        session_json=json.dumps(session, indent=2),
        match_results_json=json.dumps(match_results[:30], indent=2),
        audit_json=json.dumps(audit_entries[:30], indent=2),
        gl_entries_json=json.dumps(relevant_gl[:20], indent=2),
    )

    client = _get_client()
    message = client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=QA_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text.strip()


# ─── Duplicate explanation (narrative for Slack) ──────────────────────────────

DUP_SYSTEM = """You are a financial controller AI. Generate a brief, plain-English
explanation of why two GL entries are suspected duplicates. 2-3 sentences maximum.
Focus on the specific facts: vendor, amounts, dates, reference numbers, and the
most likely business reason for the discrepancy (e.g. waived fee, ACH vs internal ref)."""

def explain_duplicate(dup: dict) -> str:
    """Generate a plain-English explanation for a detected duplicate pair."""
    client = _get_client()
    prompt = (
        f"Entry A: {dup['entry_a']['vendor']} — ${dup['amount_a']:,.2f} — "
        f"ref: {dup['entry_a']['reference_number']} — {dup['entry_a']['date']}\n"
        f"Entry B: {dup['entry_b']['vendor']} — ${dup['amount_b']:,.2f} — "
        f"ref: {dup['entry_b']['reference_number']} — {dup['entry_b']['date']}\n"
        f"Amount variance: {dup['amount_variance_pct']}%  |  Days apart: {dup['days_apart']}\n\n"
        f"Explain why these are likely the same transaction."
    )
    message = client.messages.create(
        model=MODEL,
        max_tokens=150,
        system=DUP_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()
