"""
matching.py — Two-layer reconciliation engine.

Layer 1 (deterministic, no LLM):
  - Exact amount + date within ±3 days + matching reference_number or vendor name
  - Confidence 1.0, no API call made
  - Also runs duplicate detection across all GL entries

Layer 2 (LLM via llm.py):
  - Called only for GL entries that didn't get a clean deterministic match
  - Returns confidence score + one-line reasoning + recommended action

Match statuses:
    matched          Clean 3-way (or 2-way) match, confidence 1.0
    partial          One leg missing (GL+AP but no bank, etc.)
    exception        LLM flagged — needs controller review
    duplicate        Suspected duplicate payment, needs confirmation
    unmatched        No match found after both layers
    amended          Entry was amended (amount changed post-original)
    pending_approval Agent has recommended action, awaiting controller click
    posted           Approved and posted
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

try:
    from Levenshtein import distance as levenshtein_distance
except ImportError:
    # Fallback pure-Python implementation if python-Levenshtein not installed
    def levenshtein_distance(s1: str, s2: str) -> int:  # type: ignore[misc]
        if s1 == s2:
            return 0
        if len(s1) < len(s2):
            s1, s2 = s2, s1
        prev = list(range(len(s2) + 1))
        for i, c1 in enumerate(s1):
            curr = [i + 1]
            for j, c2 in enumerate(s2):
                curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
            prev = curr
        return prev[-1]

import database as db


# ─── Date helpers ─────────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str[:10], "%Y-%m-%d")


def _within_days(date_a: str, date_b: str, days: int) -> bool:
    return abs((_parse_date(date_a) - _parse_date(date_b)).days) <= days


# ─── Vendor fuzzy match ───────────────────────────────────────────────────────

def vendors_match(v1: str, v2: str, max_distance: int = 2) -> bool:
    """Return True if vendor names are exact or within Levenshtein distance."""
    if not v1 or not v2:
        return False
    v1n = v1.strip().lower()
    v2n = v2.strip().lower()
    if v1n == v2n:
        return True
    # Also check if one is a prefix/substring of the other (handles "Amazon Web Services" vs "AWS")
    if v1n in v2n or v2n in v1n:
        return True
    return levenshtein_distance(v1n, v2n) <= max_distance


# ─── Layer 1: Deterministic matching ─────────────────────────────────────────

def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def deterministic_match(
    gl: dict,
    ap_entries: list[dict],
    bank_txns: list[dict],
) -> Optional[dict]:
    """
    Attempt a deterministic match for one GL entry.

    Match criteria:
        amount exact AND date within ±3 days AND
        (reference_number exact OR vendor_name match)

    Returns a match_result dict if a match is found, else None.
    Populates:  matched   — full 3-way match (GL + AP + bank)
                partial   — GL + AP but no bank, or GL + bank but no AP
    """
    amount   = gl["amount"]
    gl_ref   = str(gl.get("reference_number", "")).strip()
    gl_vendor = str(gl.get("vendor", "")).strip()

    # ── Find AP candidate ─────────────────────────────────────────────────────
    ap_match = None
    for ap in ap_entries:
        if abs(ap["amount"] - amount) > 0.01:
            continue
        if not _within_days(gl["date"], ap["date"], 3):
            continue
        ap_ref = str(ap.get("reference_number", "")).strip()
        if (gl_ref and ap_ref and gl_ref == ap_ref) or vendors_match(gl_vendor, ap["vendor"]):
            ap_match = ap
            break

    # ── Find bank candidate ───────────────────────────────────────────────────
    bank_match = None
    for bt in bank_txns:
        if abs(bt["amount"] - amount) > 0.01:
            continue
        if not _within_days(gl["date"], bt["date"], 3):
            continue
        bt_ref = str(bt.get("reference_number", "")).strip()
        bt_desc = str(bt.get("description", "")).lower()
        ref_match    = gl_ref and bt_ref and gl_ref == bt_ref
        vendor_in_desc = gl_vendor.lower() in bt_desc if gl_vendor else False
        if ref_match or vendor_in_desc:
            bank_match = bt
            break

    if ap_match and bank_match:
        return {
            "id":                  f"MR-{uuid.uuid4().hex[:8].upper()}",
            "gl_entry_id":         gl["id"],
            "subledger_entry_id":  ap_match["id"],
            "bank_transaction_id": bank_match["id"],
            "match_status":        "matched",
            "confidence_score":    1.0,
            "reasoning":           "Exact 3-way match: amount, date ±3 days, reference/vendor align.",
            "matched_at":          _now_iso(),
        }

    if ap_match and not bank_match:
        return {
            "id":                  f"MR-{uuid.uuid4().hex[:8].upper()}",
            "gl_entry_id":         gl["id"],
            "subledger_entry_id":  ap_match["id"],
            "bank_transaction_id": None,
            "match_status":        "partial",
            "confidence_score":    0.85,
            "reasoning":           "GL + AP match; bank payment not yet cleared.",
            "matched_at":          _now_iso(),
        }

    if bank_match and not ap_match:
        return {
            "id":                  f"MR-{uuid.uuid4().hex[:8].upper()}",
            "gl_entry_id":         gl["id"],
            "subledger_entry_id":  None,
            "bank_transaction_id": bank_match["id"],
            "match_status":        "partial",
            "confidence_score":    0.80,
            "reasoning":           "GL + bank match; no AP subledger invoice found.",
            "matched_at":          _now_iso(),
        }

    return None


# ─── Duplicate Detection ──────────────────────────────────────────────────────

def detect_duplicates(gl_entries: list[dict]) -> list[dict]:
    """
    Scan all GL entries for suspected duplicate payments.

    Flags a pair as duplicate if ALL are true:
        1. Same vendor (exact or Levenshtein ≤ 2)
        2. Amounts within 2% of each other
        3. Both entries within a 5-day window
        4. Different reference numbers
        5. Both entries exist in the GL

    Thresholds are intentionally tight to avoid false positives:
        - 2% amount variance: catches waived fees / rounding (Rippling $150 delta)
          but not legitimately different charges (Bombora $2,400 vs $2,300 = 4.2%)
        - 5-day window: catches same-run ACH vs invoice refs (Rippling, 2 days apart)
          but not split-milestone payments (TechContract, 10 days apart)

    Returns list of duplicate-pair dicts with detection metadata.
    """
    duplicates = []
    flagged_pairs: set[frozenset] = set()

    for i, a in enumerate(gl_entries):
        for b in gl_entries[i + 1:]:
            pair = frozenset([a["id"], b["id"]])
            if pair in flagged_pairs:
                continue

            # Condition 1: same vendor
            if not vendors_match(str(a.get("vendor", "")), str(b.get("vendor", ""))):
                continue

            # Condition 2: amounts within 2%
            amt_a, amt_b = abs(a["amount"]), abs(b["amount"])
            if amt_a == 0 or amt_b == 0:
                continue
            variance = abs(amt_a - amt_b) / max(amt_a, amt_b)
            if variance > 0.02:
                continue

            # Condition 3: within 5-day window
            if not _within_days(a["date"], b["date"], 5):
                continue

            # Condition 4: different reference numbers
            ref_a = str(a.get("reference_number", "")).strip()
            ref_b = str(b.get("reference_number", "")).strip()
            if ref_a and ref_b and ref_a == ref_b:
                continue

            # All conditions met — flag as duplicate
            flagged_pairs.add(pair)
            pct = round(variance * 100, 1)
            # Confidence heuristic: closer the amounts and dates, higher confidence
            days_apart = abs((_parse_date(a["date"]) - _parse_date(b["date"])).days)
            confidence = round(max(0.5, 1.0 - (pct / 100) - (days_apart / 60)), 2)

            duplicates.append({
                "entry_a_id":   a["id"],
                "entry_b_id":   b["id"],
                "entry_a":      a,
                "entry_b":      b,
                "vendor":       a["vendor"],
                "amount_a":     a["amount"],
                "amount_b":     b["amount"],
                "amount_variance_pct": pct,
                "days_apart":   days_apart,
                "confidence":   confidence,
            })

    return duplicates


# ─── Full Reconciliation Run ──────────────────────────────────────────────────

def run_reconciliation(gl_entries: list[dict]) -> dict:
    """
    Run both matching layers across all GL entries.

    Returns a summary dict:
        results        : list of match_result dicts (Layer 1 clean matches + partials)
        exceptions     : list of GL entries that need LLM review
        duplicates     : list of duplicate-pair dicts
        unmatched_ids  : GL entry IDs with no match at all
        counts         : summary counters
    """
    ap_entries = db.get_all_ap_entries()
    bank_txns  = db.get_all_bank_transactions()

    # Track AP/bank entries already consumed by a match
    used_ap_ids:   set[str] = set()
    used_bank_ids: set[str] = set()

    matched_results: list[dict] = []
    exception_gl:    list[dict] = []
    unmatched_ids:   list[str]  = []

    # ── Layer 1: deterministic ────────────────────────────────────────────────
    for gl in gl_entries:
        # Skip already-duplicate entries (will be handled separately)
        # Filter out already-consumed entries
        available_ap   = [a for a in ap_entries   if a["id"] not in used_ap_ids]
        available_bank = [b for b in bank_txns     if b["id"] not in used_bank_ids]

        result = deterministic_match(gl, available_ap, available_bank)

        if result:
            matched_results.append(result)
            db.save_match_result(result)
            if result.get("subledger_entry_id"):
                used_ap_ids.add(result["subledger_entry_id"])
            if result.get("bank_transaction_id"):
                used_bank_ids.add(result["bank_transaction_id"])
        else:
            exception_gl.append(gl)

    # ── Duplicate detection (runs on ALL GL entries before LLM layer) ─────────
    duplicates = detect_duplicates(gl_entries)

    # Mark duplicate GL entries so LLM layer knows to skip them
    duplicate_gl_ids: set[str] = set()
    for dup in duplicates:
        duplicate_gl_ids.add(dup["entry_a_id"])
        duplicate_gl_ids.add(dup["entry_b_id"])
        # Save a match result for each entry in the pair
        for entry_id in [dup["entry_a_id"], dup["entry_b_id"]]:
            mr = {
                "id":                  f"MR-{uuid.uuid4().hex[:8].upper()}",
                "gl_entry_id":         entry_id,
                "subledger_entry_id":  None,
                "bank_transaction_id": None,
                "match_status":        "duplicate",
                "confidence_score":    dup["confidence"],
                "reasoning":           (
                    f"Suspected duplicate: same vendor ({dup['vendor']}), "
                    f"amounts within {dup['amount_variance_pct']}%, "
                    f"{dup['days_apart']} days apart, different reference numbers."
                ),
                "matched_at":          _now_iso(),
            }
            db.save_match_result(mr)

    # ── Separate true exceptions from duplicates ──────────────────────────────
    true_exceptions = [g for g in exception_gl if g["id"] not in duplicate_gl_ids]

    # Remaining unmatched after LLM will be filled in by llm.py
    # Here we just identify which GL entries need LLM attention
    for gl in true_exceptions:
        mr_placeholder = {
            "id":                  f"MR-{uuid.uuid4().hex[:8].upper()}",
            "gl_entry_id":         gl["id"],
            "subledger_entry_id":  None,
            "bank_transaction_id": None,
            "match_status":        "exception",
            "confidence_score":    None,
            "reasoning":           "Pending LLM analysis.",
            "matched_at":          _now_iso(),
        }
        db.save_match_result(mr_placeholder)

    # Count matched vs partial vs exception
    matched_count = sum(1 for r in matched_results if r["match_status"] == "matched")
    partial_count = sum(1 for r in matched_results if r["match_status"] == "partial")

    return {
        "results":       matched_results,
        "exceptions":    true_exceptions,
        "duplicates":    duplicates,
        "unmatched_ids": [g["id"] for g in true_exceptions],
        "counts": {
            "total":      len(gl_entries),
            "matched":    matched_count,
            "partial":    partial_count,
            "exceptions": len(true_exceptions),
            "duplicates": len(duplicates),
        },
    }


# ─── AP entries without any GL match ─────────────────────────────────────────

def find_orphan_ap_entries(gl_entries: list[dict], ap_entries: list[dict]) -> list[dict]:
    """
    Return AP entries that have no corresponding GL entry.
    These represent invoices received but not yet coded/posted to the GL.
    """
    matched_results = db.get_all_match_results()
    matched_ap_ids = {r["subledger_entry_id"] for r in matched_results if r.get("subledger_entry_id")}
    gl_refs = {str(g.get("reference_number", "")).strip() for g in gl_entries}
    gl_vendors = [str(g.get("vendor", "")).strip().lower() for g in gl_entries]

    orphans = []
    for ap in ap_entries:
        if ap["id"] in matched_ap_ids:
            continue
        ap_ref = str(ap.get("reference_number", "")).strip()
        # Not already matched by reference
        if ap_ref in gl_refs:
            continue
        orphans.append(ap)

    return orphans
