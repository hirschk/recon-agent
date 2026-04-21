"""
generate_fixtures.py — Generate synthetic CSV fixtures and pre-seed the audit log.

Run once before starting the bot:
    python data/generate_fixtures.py

Creates:
    data/march_gl_export.csv       (52 rows)
    data/march_ap_subledger.csv    (~55 rows)
    data/march_bank_statement.csv  (~20 rows)

Also pre-seeds the database with:
    - Scenario A: GL-0023 AWS credits amendment (pre-existing audit event)
    - Scenario B template data (GL-0031 HubSpot seat expansion, pending)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import csv
from datetime import datetime, timezone
from pathlib import Path

# Bootstrap DB before importing modules that need it
from dotenv import load_dotenv

load_dotenv()

import database as db

DATA_DIR = Path(__file__).parent
MARCH = "2024-03"


# ─── Helper ───────────────────────────────────────────────────────────────────

def d(day: int) -> str:
    return f"2024-03-{day:02d}"


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Wrote {len(rows)} rows → {path.name}")


# ─── GL Entries (52 rows) ─────────────────────────────────────────────────────
#
# Status key:
#   pending          = loaded, not yet reconciled
#   matched          = clean 3-way (or 2-way) match
#   partial          = one leg missing
#   exception        = LLM-flagged, needs review
#   amended          = amount changed post-original-entry
#   duplicate        = suspected duplicate payment
#   unmatched        = no match found
#   pending_approval = agent has recommended action, awaiting controller click
#   posted           = approved and posted
#

GL_ENTRIES = [
    # ── 6010 Software & SaaS ── 13 clean matches ─────────────────────────────
    {"id": "GL-0001", "date": d(1),  "amount": 24000.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "Salesforce CRM — annual renewal",               "vendor": "Salesforce",         "reference_number": "SF-2024-001",   "status": "pending"},
    {"id": "GL-0002", "date": d(1),  "amount":  1800.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "HubSpot Marketing Hub — monthly",               "vendor": "HubSpot",            "reference_number": "HB-2024-202",   "status": "pending"},
    {"id": "GL-0003", "date": d(3),  "amount":  1200.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "Slack Business+ — monthly",                     "vendor": "Slack",              "reference_number": "SL-2024-031",   "status": "pending"},
    {"id": "GL-0004", "date": d(4),  "amount":   800.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "Figma Organization — monthly seats",            "vendor": "Figma",              "reference_number": "FIG-2024-031",  "status": "pending"},
    {"id": "GL-0005", "date": d(5),  "amount":   400.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "Linear Team — monthly",                         "vendor": "Linear",             "reference_number": "LIN-2024-031",  "status": "pending"},
    {"id": "GL-0006", "date": d(6),  "amount":   320.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "Notion Team — monthly",                         "vendor": "Notion",             "reference_number": "NOT-2024-031",  "status": "pending"},
    {"id": "GL-0007", "date": d(8),  "amount":   600.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "Zoom Business — monthly",                       "vendor": "Zoom",               "reference_number": "ZM-2024-031",   "status": "pending"},
    {"id": "GL-0008", "date": d(10), "amount":   240.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "Loom Business — monthly",                       "vendor": "Loom",               "reference_number": "LM-2024-031",   "status": "pending"},
    {"id": "GL-0009", "date": d(12), "amount":  1400.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "Intercom Starter — monthly",                    "vendor": "Intercom",           "reference_number": "IC-2024-031",   "status": "pending"},
    {"id": "GL-0010", "date": d(15), "amount":   420.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "GitHub Enterprise — monthly",                   "vendor": "GitHub",             "reference_number": "GH-2024-031",   "status": "pending"},
    {"id": "GL-0011", "date": d(18), "amount":   900.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "Retool Team — monthly",                         "vendor": "Retool",             "reference_number": "RT-2024-031",   "status": "pending"},
    {"id": "GL-0012", "date": d(20), "amount":   350.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "PostHog Scale — monthly",                       "vendor": "PostHog",            "reference_number": "PH-2024-031",   "status": "pending"},
    {"id": "GL-0013", "date": d(22), "amount":   800.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "PagerDuty Professional — monthly",              "vendor": "PagerDuty",          "reference_number": "PD-2024-031",   "status": "pending"},

    # ── 6020 Cloud Infrastructure ── 8 clean matches ──────────────────────────
    {"id": "GL-0014", "date": d(1),  "amount":  8200.00, "account_code": "6020", "account_name": "Cloud Infrastructure",    "description": "AWS — EC2, S3, RDS March usage",                "vendor": "Amazon Web Services", "reference_number": "AWS-2024-031-A", "status": "pending"},
    {"id": "GL-0015", "date": d(3),  "amount":  3400.00, "account_code": "6020", "account_name": "Cloud Infrastructure",    "description": "GCP — BigQuery, Cloud Run March",               "vendor": "Google Cloud",       "reference_number": "GCP-2024-031",  "status": "pending"},
    {"id": "GL-0016", "date": d(4),  "amount":  2800.00, "account_code": "6020", "account_name": "Cloud Infrastructure",    "description": "Datadog — infrastructure monitoring",           "vendor": "Datadog",            "reference_number": "DD-2024-031",   "status": "pending"},
    {"id": "GL-0017", "date": d(8),  "amount":   400.00, "account_code": "6020", "account_name": "Cloud Infrastructure",    "description": "Cloudflare Pro — CDN & security",              "vendor": "Cloudflare",         "reference_number": "CF-2024-031",   "status": "pending"},
    {"id": "GL-0018", "date": d(12), "amount":  3600.00, "account_code": "6020", "account_name": "Cloud Infrastructure",    "description": "Snowflake — data warehouse March compute",      "vendor": "Snowflake",          "reference_number": "SNW-2024-031",  "status": "pending"},
    {"id": "GL-0019", "date": d(15), "amount":   480.00, "account_code": "6020", "account_name": "Cloud Infrastructure",    "description": "Vercel Pro — frontend hosting",                 "vendor": "Vercel",             "reference_number": "VCL-2024-031",  "status": "pending"},
    {"id": "GL-0020", "date": d(20), "amount":  1200.00, "account_code": "6020", "account_name": "Cloud Infrastructure",    "description": "MongoDB Atlas M30 — March usage",               "vendor": "MongoDB",            "reference_number": "MDB-2024-031",  "status": "pending"},
    {"id": "GL-0021", "date": d(25), "amount":   640.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "Asana Business — monthly seats",                "vendor": "Asana",              "reference_number": "ASN-2024-031",  "status": "pending"},

    # ── 6030 Contractor & Consulting ── 5 clean matches ───────────────────────
    {"id": "GL-0022", "date": d(1),  "amount":  8000.00, "account_code": "6030", "account_name": "Contractor & Consulting", "description": "John Smith — backend engineering retainer",     "vendor": "John Smith Consulting", "reference_number": "JSC-2024-031", "status": "pending"},
    # GL-0023 is the AWS amendment scenario (LLM exception)
    {"id": "GL-0023", "date": d(15), "amount":  4200.00, "account_code": "6020", "account_name": "Cloud Infrastructure",    "description": "AWS — reserved instances, credits pending",     "vendor": "Amazon Web Services", "reference_number": "AWS-2024-031-B", "status": "pending"},
    {"id": "GL-0024", "date": d(8),  "amount": 12000.00, "account_code": "6030", "account_name": "Contractor & Consulting", "description": "Acme Dev LLC — product sprint February",        "vendor": "Acme Dev LLC",       "reference_number": "ADL-2024-031",  "status": "pending"},
    {"id": "GL-0025", "date": d(15), "amount":  6400.00, "account_code": "6030", "account_name": "Contractor & Consulting", "description": "DataSync Consulting — data migration work",     "vendor": "DataSync Consulting", "reference_number": "DSC-2024-031", "status": "pending"},
    {"id": "GL-0026", "date": d(20), "amount":  4200.00, "account_code": "6030", "account_name": "Contractor & Consulting", "description": "Growth Advisors LLC — go-to-market strategy",   "vendor": "Growth Advisors LLC", "reference_number": "GA-2024-031",  "status": "pending"},

    # ── 6040 Marketing & Advertising ── 5 clean matches ──────────────────────
    {"id": "GL-0027", "date": d(1),  "amount": 15000.00, "account_code": "6040", "account_name": "Marketing & Advertising", "description": "Google Ads — paid search March budget",         "vendor": "Google Ads",         "reference_number": "GAD-2024-031",  "status": "pending"},
    {"id": "GL-0028", "date": d(5),  "amount":  8000.00, "account_code": "6040", "account_name": "Marketing & Advertising", "description": "LinkedIn Ads — demand generation campaign",     "vendor": "LinkedIn",           "reference_number": "LI-2024-031",   "status": "pending"},
    {"id": "GL-0029", "date": d(8),  "amount":  1200.00, "account_code": "6040", "account_name": "Marketing & Advertising", "description": "Clearbit — enrichment API usage",              "vendor": "Clearbit",           "reference_number": "CB-2024-031",   "status": "pending"},
    {"id": "GL-0030", "date": d(12), "amount":   800.00, "account_code": "6040", "account_name": "Marketing & Advertising", "description": "Semrush Business — SEO tooling",                "vendor": "Semrush",            "reference_number": "SEM-2024-031",  "status": "pending"},
    # GL-0031 is the HubSpot seat expansion (human-in-the-loop scenario)
    # Date is March 20 — the invoice arrived March 14 but wasn't coded until March 20.
    # The 6-day gap ensures it misses the ±3-day deterministic window against AP-0045
    # (March 14), falling through to the LLM layer where the agent asks for approval.
    {"id": "GL-0031", "date": d(20), "amount":  2400.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "HubSpot — seat expansion Q1, mid-month invoice", "vendor": "HubSpot",           "reference_number": "HB-2291",       "status": "pending"},

    # ── 6050 Payroll Processing ── 4 entries (2 clean, 2 duplicate pair) ──────
    {"id": "GL-0032", "date": d(1),  "amount": 45000.00, "account_code": "6050", "account_name": "Payroll Processing",      "description": "Rippling — payroll run March 1 cycle",          "vendor": "Rippling",           "reference_number": "RIP-2024-MC1",  "status": "pending"},
    {"id": "GL-0033", "date": d(15), "amount": 45200.00, "account_code": "6050", "account_name": "Payroll Processing",      "description": "Rippling — payroll run March 15 cycle",         "vendor": "Rippling",           "reference_number": "RIP-2024-MC2",  "status": "pending"},
    # Duplicate pair — same payroll run, two entries (flagged by duplicate detection)
    {"id": "GL-0034", "date": d(5),  "amount": 12400.00, "account_code": "6050", "account_name": "Payroll Processing",      "description": "Rippling — employer payroll taxes Q1",          "vendor": "Rippling",           "reference_number": "RIP-2024-031",  "status": "pending"},
    {"id": "GL-0035", "date": d(7),  "amount": 12250.00, "account_code": "6050", "account_name": "Payroll Processing",      "description": "Rippling — employer payroll taxes Q1 ACH",     "vendor": "Rippling",           "reference_number": "ACH-887341",    "status": "pending"},

    # ── 6060 Legal & Compliance ── 4 clean matches ────────────────────────────
    {"id": "GL-0036", "date": d(3),  "amount":  2400.00, "account_code": "6060", "account_name": "Legal & Compliance",      "description": "Ironclad — contract lifecycle platform",        "vendor": "Ironclad",           "reference_number": "IC-2024-031",   "status": "pending"},
    {"id": "GL-0037", "date": d(8),  "amount": 18000.00, "account_code": "6060", "account_name": "Legal & Compliance",      "description": "Wilson Sonsini — Series B due diligence",       "vendor": "Wilson Sonsini",     "reference_number": "WS-2024-0312",  "status": "pending"},
    {"id": "GL-0038", "date": d(15), "amount": 12500.00, "account_code": "6060", "account_name": "Legal & Compliance",      "description": "Goodwin Law — IP protection & patents",         "vendor": "Goodwin Law",        "reference_number": "GL-2024-0315",  "status": "pending"},
    {"id": "GL-0039", "date": d(20), "amount":   600.00, "account_code": "6060", "account_name": "Legal & Compliance",      "description": "Carta — cap table management",                  "vendor": "Carta",              "reference_number": "CRT-2024-031",  "status": "pending"},

    # ── 6070 Travel & Expenses ── 3 clean matches ─────────────────────────────
    {"id": "GL-0040", "date": d(10), "amount":  3200.00, "account_code": "6070", "account_name": "Travel & Expenses",       "description": "Airbnb — team offsite Tahoe 3 nights",          "vendor": "Airbnb",             "reference_number": "AIR-2024-031",  "status": "pending"},
    {"id": "GL-0041", "date": d(12), "amount":  4800.00, "account_code": "6070", "account_name": "Travel & Expenses",       "description": "United Airlines — SF to NYC sales trip (4 pax)", "vendor": "United Airlines",   "reference_number": "UA-2024-0312",  "status": "pending"},
    {"id": "GL-0042", "date": d(18), "amount":  2100.00, "account_code": "6070", "account_name": "Travel & Expenses",       "description": "Marriott — sales team NYC accommodation",       "vendor": "Marriott",           "reference_number": "MAR-2024-031",  "status": "pending"},

    # ── More 6030 clean matches ────────────────────────────────────────────────
    {"id": "GL-0043", "date": d(5),  "amount":  5500.00, "account_code": "6030", "account_name": "Contractor & Consulting", "description": "Jane Doe — UX design contract March",           "vendor": "Jane Doe Freelance", "reference_number": "JDF-2024-031",  "status": "pending"},
    {"id": "GL-0044", "date": d(18), "amount":  2400.00, "account_code": "6040", "account_name": "Marketing & Advertising", "description": "Bombora — intent data subscription",            "vendor": "Bombora",            "reference_number": "BB-2024-031",   "status": "pending"},

    # ── Partial matches (3) — one leg missing ─────────────────────────────────
    {"id": "GL-0045", "date": d(22), "amount":  3200.00, "account_code": "6020", "account_name": "Cloud Infrastructure",    "description": "Datadog — APM add-on, bank payment pending",    "vendor": "Datadog",            "reference_number": "DD-2024-032",   "status": "pending"},
    {"id": "GL-0046", "date": d(25), "amount":   720.00, "account_code": "6020", "account_name": "Cloud Infrastructure",    "description": "Vercel Enterprise — add-on seats",              "vendor": "Vercel",             "reference_number": "VCL-2024-032",  "status": "pending"},
    {"id": "GL-0047", "date": d(28), "amount":   480.00, "account_code": "6010", "account_name": "Software & SaaS",         "description": "SendGrid — transactional email (cleared bank)", "vendor": "SendGrid",           "reference_number": "SG-2024-031",   "status": "pending"},

    # ── Contractor split payment (LLM exception) ──────────────────────────────
    {"id": "GL-0048", "date": d(10), "amount":  4800.00, "account_code": "6030", "account_name": "Contractor & Consulting", "description": "TechContract LLC — milestone 1 of 2",           "vendor": "TechContract LLC",   "reference_number": "TCL-2024-031A", "status": "pending"},
    {"id": "GL-0049", "date": d(20), "amount":  4800.00, "account_code": "6030", "account_name": "Contractor & Consulting", "description": "TechContract LLC — milestone 2 of 2",           "vendor": "TechContract LLC",   "reference_number": "TCL-2024-031B", "status": "pending"},

    # ── Unmatched entries (2) ─────────────────────────────────────────────────
    {"id": "GL-0050", "date": d(19), "amount":  1847.00, "account_code": "6030", "account_name": "Contractor & Consulting", "description": "Unknown ACH debit — no vendor identified",      "vendor": "Unknown",            "reference_number": "ACH-993847",    "status": "pending"},
    {"id": "GL-0051", "date": d(21), "amount":  2300.00, "account_code": "6040", "account_name": "Marketing & Advertising", "description": "ACH debit — vendor ACH details expired",        "vendor": "Bombora",            "reference_number": "ACH-004521",    "status": "pending"},

    # ── Figma prorated refund (1) ─────────────────────────────────────────────
    {"id": "GL-0052", "date": d(14), "amount": -480.00,  "account_code": "6010", "account_name": "Software & SaaS",         "description": "Figma — prorated refund, seat cancellation",    "vendor": "Figma",              "reference_number": "FIG-CR-2024-031", "status": "pending"},
]

assert len(GL_ENTRIES) == 52, f"Expected 52 GL entries, got {len(GL_ENTRIES)}"

GL_FIELDS = ["id", "date", "amount", "account_code", "account_name", "description", "vendor", "reference_number", "status"]

# IDs of clean-match GL entries (used to build AP + bank counterparts)
CLEAN_GL_IDS = [
    "GL-0001", "GL-0002", "GL-0003", "GL-0004", "GL-0005",
    "GL-0006", "GL-0007", "GL-0008", "GL-0009", "GL-0010",
    "GL-0011", "GL-0012", "GL-0013",  # 6010
    "GL-0014", "GL-0015", "GL-0016", "GL-0017", "GL-0018",
    "GL-0019", "GL-0020", "GL-0021",  # 6020 + misc
    "GL-0022", "GL-0024", "GL-0025", "GL-0026",  # 6030 (skip GL-0023 = AWS exception)
    "GL-0027", "GL-0028", "GL-0029", "GL-0030",  # 6040 (skip GL-0031 = HubSpot HITL)
    "GL-0032", "GL-0033",            # 6050 regular payroll
    "GL-0036", "GL-0037", "GL-0038", "GL-0039",  # 6060
    "GL-0040", "GL-0041", "GL-0042",  # 6070
    "GL-0043", "GL-0044",            # contractor + marketing
]
assert len(CLEAN_GL_IDS) == 40, f"Expected 40 clean GL IDs, got {len(CLEAN_GL_IDS)}"


# ─── AP Subledger (~55 rows) ──────────────────────────────────────────────────

def _drive_url(seed: str) -> str:
    """
    Generate a deterministic fake Google Drive URL from a seed string.
    Format matches real Drive share links: /file/d/<33-char alphanum>/view
    Deterministic so URLs are stable across fixture regenerations.
    """
    import hashlib
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    # SHA-256 = 32 bytes = 64 hex chars; step by 2 gives 32 pairs, then one extra nibble
    digest = hashlib.sha256(seed.encode()).hexdigest()  # 64 chars
    file_id = "".join(chars[int(digest[i:i+2], 16) % len(chars)] for i in range(0, 64, 2))
    # Append one extra char from the first nibble to reach 33 chars
    file_id += chars[int(digest[0], 16) % len(chars)]
    return f"https://drive.google.com/file/d/{file_id}/view"


def build_ap_entries(gl_map: dict) -> list[dict]:
    ap = []

    # Mirror clean GL entries with matching AP invoices
    for i, gl_id in enumerate(CLEAN_GL_IDS):
        g = gl_map[gl_id]
        inv_num = f"INV-{gl_id}-M"
        ap.append({
            "id":               f"AP-{i+1:04d}",
            "date":             g["date"],
            "amount":           g["amount"],
            "vendor":           g["vendor"],
            "gl_account":       g["account_code"],
            "invoice_number":   inv_num,
            "status":           "open",
            "reference_number": g["reference_number"],
            "invoice_url":      _drive_url(inv_num),
        })

    # Partial match A: Datadog APM add-on (GL-0045) — AP exists, bank not cleared
    ap.append({
        "id": "AP-0042", "date": "2024-03-22", "amount": 3200.00,
        "vendor": "Datadog", "gl_account": "6020", "invoice_number": "DD-INV-0422",
        "status": "open", "reference_number": "DD-2024-032",
        "invoice_url": _drive_url("DD-INV-0422"),
    })

    # Partial match B: Vercel (GL-0046) — AP exists, bank not cleared
    ap.append({
        "id": "AP-0043", "date": "2024-03-25", "amount": 720.00,
        "vendor": "Vercel", "gl_account": "6020", "invoice_number": "VCL-INV-0425",
        "status": "open", "reference_number": "VCL-2024-032",
        "invoice_url": _drive_url("VCL-INV-0425"),
    })

    # Partial match C: SendGrid (GL-0047) — no AP, bank cleared directly (no entry here)

    # AWS exception (GL-0023): AP says $3,900 — amount mismatch with GL $4,200
    ap.append({
        "id": "AP-0044", "date": "2024-03-15", "amount": 3900.00,
        "vendor": "Amazon Web Services", "gl_account": "6020",
        "invoice_number": "AWS-INV-0315-B", "status": "open",
        "reference_number": "AWS-2024-031-B",
        "invoice_url": "https://drive.google.com/file/d/12yINcjHjA5JS6_Iyuo049bl6zEWt8Gvq/view?usp=sharing",
    })

    # HubSpot seat expansion (GL-0031): AP invoice exists, awaiting GL posting
    ap.append({
        "id": "AP-0045", "date": "2024-03-14", "amount": 2400.00,
        "vendor": "HubSpot", "gl_account": "6010",
        "invoice_number": "HB-2291", "status": "open",
        "reference_number": "HB-2291",
        "invoice_url": _drive_url("HB-2291"),
    })

    # TechContract LLC: one AP invoice for $9,600 split into two GL payments
    ap.append({
        "id": "AP-0046", "date": "2024-03-01", "amount": 9600.00,
        "vendor": "TechContract LLC", "gl_account": "6030",
        "invoice_number": "TCL-2024-031", "status": "open",
        "reference_number": "TCL-2024-031",
        "invoice_url": _drive_url("TCL-2024-031"),
    })

    # No AP entry for the Rippling duplicate pair.
    # GL-0034 and GL-0035 are both GL entries with no backing AP invoice — the entire
    # point of the duplicate scenario is that both look legitimate in the GL on their
    # own. Providing an AP match for GL-0034 would cause it to resolve as a clean
    # partial before the duplicate detector runs, defeating the demo.

    # Figma refund AP credit note
    ap.append({
        "id": "AP-0048", "date": "2024-03-14", "amount": -480.00,
        "vendor": "Figma", "gl_account": "6010",
        "invoice_number": "FIG-CN-2024-031", "status": "open",
        "reference_number": "FIG-CR-2024-031",
        "invoice_url": _drive_url("FIG-CN-2024-031"),
    })

    return ap


AP_FIELDS = ["id", "date", "amount", "vendor", "gl_account", "invoice_number", "status", "reference_number", "invoice_url"]


# ─── Bank Statement (~20 rows) ────────────────────────────────────────────────

def build_bank_transactions(gl_map: dict) -> list[dict]:
    # All clean GL entries that have ACH/wire bank transactions.
    # Intentionally EXCLUDED (no bank transaction — partial match by design):
    #   GL-0009  Intercom   — billed to corporate card, not ACH
    #   GL-0010  GitHub     — billed to corporate card, not ACH
    #   GL-0011  Retool     — billed to corporate card, not ACH
    #   GL-0012  PostHog    — billed to corporate card, not ACH
    #   GL-0044  Bombora    — wire transfer still in transit
    # That leaves 5 clean partials + 3 explicitly-partial GL entries = 8 total partials.
    bank_gl_ids = [
        # 6010 Software & SaaS
        "GL-0001", "GL-0002", "GL-0003", "GL-0004", "GL-0005",
        "GL-0006", "GL-0007", "GL-0008", "GL-0013", "GL-0021",
        # 6020 Cloud Infrastructure
        "GL-0014", "GL-0015", "GL-0016", "GL-0017", "GL-0018",
        "GL-0019", "GL-0020",
        # 6030 Contractor & Consulting
        "GL-0022", "GL-0024", "GL-0025", "GL-0026", "GL-0043",
        # 6040 Marketing & Advertising
        "GL-0027", "GL-0028", "GL-0029", "GL-0030",
        # 6050 Payroll Processing
        "GL-0032", "GL-0033",
        # 6060 Legal & Compliance
        "GL-0036", "GL-0037", "GL-0038", "GL-0039",
        # 6070 Travel & Expenses
        "GL-0040", "GL-0041", "GL-0042",
    ]
    txns = []
    for i, gl_id in enumerate(bank_gl_ids):
        g = gl_map[gl_id]
        txns.append({
            "id":               f"BNK-{i+1:04d}",
            "date":             g["date"],
            "amount":           g["amount"],
            "description":      f"ACH OUT {g['vendor'].upper()[:20]}",
            "reference_number": g["reference_number"],
        })

    # SendGrid (partial match GL-0047): bank cleared, no AP
    txns.append({
        "id": f"BNK-{len(bank_gl_ids)+1:04d}",
        "date": "2024-03-28", "amount": 480.00,
        "description": "ACH OUT SENDGRID", "reference_number": "SG-2024-031",
    })

    # Mystery ACH (GL-0050 counterpart in bank)
    txns.append({
        "id": f"BNK-{len(bank_gl_ids)+2:04d}",
        "date": "2024-03-19", "amount": 1847.00,
        "description": "ACH OUT UNKNOWN VENDOR", "reference_number": "ACH-993847",
    })

    return txns


BANK_FIELDS = ["id", "date", "amount", "description", "reference_number"]


# ─── Pre-seed Audit Log ───────────────────────────────────────────────────────

def seed_audit_log() -> None:
    # Scenario A: AWS amendment — amount changed $4,200 → $3,900
    # Pre-existing audit event (happened before the demo session)
    db.write_audit_log(
        entity_type="GLEntry",
        entity_id="GL-0023",
        field_changed="amount",
        old_value="4200.00",
        new_value="3900.00",
        actor="controller@acme.com",
        reason="AWS credits adjustment applied — credit memo CM-2024-031",
        action_type="amendment",
        timestamp="2024-03-28T14:32:00Z",
    )
    print("  Pre-seeded Scenario A: GL-0023 AWS amendment audit event")

    # Scenario B: HubSpot session creation (agent logs this when session starts)
    # The human approval event (old_value=pending → new_value=posted) is written
    # live during the demo when the controller clicks Yes. Not pre-seeded here.
    print("  Scenario B (HubSpot approval) will be written live during demo")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Initializing database...")
    db.init_db()

    print("Building fixture data...")
    gl_map = {g["id"]: g for g in GL_ENTRIES}

    ap_entries = build_ap_entries(gl_map)
    bank_txns  = build_bank_transactions(gl_map)

    print(f"  GL entries:        {len(GL_ENTRIES)}")
    print(f"  AP subledger rows: {len(ap_entries)}")
    print(f"  Bank transactions: {len(bank_txns)}")

    # Write CSVs
    write_csv(DATA_DIR / "march_gl_export.csv",    GL_ENTRIES,  GL_FIELDS)
    write_csv(DATA_DIR / "march_ap_subledger.csv", ap_entries,  AP_FIELDS)
    write_csv(DATA_DIR / "march_bank_statement.csv", bank_txns, BANK_FIELDS)

    # Load AP + bank into DB (GL entries loaded from CSV at runtime by the bot)
    print("Loading AP subledger into database...")
    import pandas as pd
    ap_df   = pd.read_csv(DATA_DIR / "march_ap_subledger.csv")
    bank_df = pd.read_csv(DATA_DIR / "march_bank_statement.csv")
    db.load_ap_entries_from_df(ap_df)
    db.load_bank_transactions_from_df(bank_df)
    print("  AP subledger loaded")
    print("  Bank transactions loaded")

    # Seed audit log
    print("Seeding audit log...")
    seed_audit_log()

    print("\nDone. Ready to run the demo.")
    print("Drop data/march_gl_export.csv into your Slack channel to start.")


if __name__ == "__main__":
    main()
