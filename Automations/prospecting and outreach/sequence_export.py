"""
sequence_export.py — Moxo BDR Sequence Export & HubSpot Enrollment Package
===========================================================================
After the rep approves contacts and research (Step 5.5 + 6), this script
assembles the outreach cadence for every approved contact and produces three
output files Claude uses to enroll them in HubSpot.

─────────────────────────────────────────────────────────────────────────────
CADENCE DEFINITIONS

FULL contact (email + phone):          EMAIL_ONLY contact:
  Day  1 — Email #1 (cold open)          Day  1 — Email #1 (cold open)
  Day  3 — Call #1  (call notes)         Day  5 — Email #2 (follow-up)
  Day  5 — Email #2 (follow-up)          Day 11 — Email #3 (value-add)
  Day  7 — Call #2  (call notes)         Day 17 — Email #4 (breakup)
  Day 11 — Email #3 (value-add)
  Day 14 — Call #3  (call notes)
  Day 17 — Email #4 (breakup)

Emails in the cadence are EXACTLY the ones generated in Step 6 and approved
by the rep. No email copy is auto-generated here.

─────────────────────────────────────────────────────────────────────────────
OUTPUTS (written to [session_dir]/)

  sequence_enrollment.json    — full cadence per contact with all copy
  hubspot_import.csv          — HubSpot-ready bulk contact import
  hubspot_tasks.json          — call + LinkedIn tasks for manage_crm_objects

─────────────────────────────────────────────────────────────────────────────
INPUT JSON:
{
  "rep_name":   "Spencer Johnson",
  "start_date": "2026-04-08",          // ISO date; defaults to today
  "contacts": [
    {
      "company":       "Acme Financial",
      "contact_name":  "Jane Smith",
      "contact_title": "COO",
      "email":         "jsmith@acmefinancial.com",
      "phone":         "+1-415-555-0198",
      "mobile":        null,
      "linkedin":      "https://linkedin.com/in/janesmith",
      "country":       "United States",
      "state":         "CA",
      "research_summary": "Acme Financial recently expanded into 3 new states...",
      "trigger_event":    "Series B funding announcement — March 2026",
      "industry":         "Financial Services",
      "emails": [
        { "step": 1, "subject": "...", "body": "..." },
        { "step": 2, "subject": "...", "body": "..." },
        { "step": 3, "subject": "...", "body": "..." },
        { "step": 4, "subject": "...", "body": "..." }
      ],
      // Optional: ZoomInfo owner ID for HubSpot
      "hubspot_owner_id": null
    }
  ]
}

USAGE:
  python scripts/sequence_export.py <input_json> <output_dir>
  python scripts/sequence_export.py <input_json> <output_dir> --quiet
"""

import sys
import json
import os
import csv
import re
from datetime import date, timedelta

# ─────────────────────────────────────────────────────────────────────────────
# Cadence blueprints
# ─────────────────────────────────────────────────────────────────────────────

# Each step: (day_offset, channel, action_key, email_step_or_none)
CADENCE_FULL = [
    (1,  "email", "email_1",      1),
    (3,  "call",  "call_1_novm",  None),
    (5,  "email", "email_2",      2),
    (7,  "call",  "call_2_vm",    None),
    (11, "email", "email_3",      3),
    (14, "call",  "call_3_final", None),
    (17, "email", "email_4",      4),
]

CADENCE_EMAIL_ONLY = [
    (1,  "email", "email_1", 1),
    (5,  "email", "email_2", 2),
    (11, "email", "email_3", 3),
    (17, "email", "email_4", 4),
]

CHANNEL_LABELS = {
    "email": "Email",
    "call":  "Call",
}

ACTION_LABELS = {
    "email_1":      "Send cold open email (Step 1)",
    "email_2":      "Send follow-up email (Step 2)",
    "email_3":      "Send value-add email (Step 3)",
    "email_4":      "Send breakup email (Step 4)",
    "call_1":       "Call attempt #1",
    "call_2":       "Call attempt #2",
    "call_3":       "Call attempt #3",
    "call_4":       "Call attempt #4",
    # Legacy keys — kept for backward compatibility with older cadence blueprints
    "call_1_novm":  "Call attempt #1",
    "call_2_vm":    "Call attempt #2",
    "call_3_final": "Call attempt #3",
}


# ─────────────────────────────────────────────────────────────────────────────
# Copy generators — call scripts
# ─────────────────────────────────────────────────────────────────────────────

def _first_name(full_name: str) -> str:
    parts = full_name.strip().split()
    return parts[0] if parts else full_name


def build_call_notes(contact: dict) -> dict:
    """
    Returns { "phone", "call_notes" } for call task bodies.
    call_notes — structured reference block (Why this person, Process,
                 How Moxo Helps, Business Outcome, Without Moxo, Social Proof)
                 pulled from the contact's pre-approved account intelligence.
    """
    phone = contact.get("phone") or contact.get("mobile", "")

    # Call notes fields are expected to be pre-populated by the SKILL.md
    # workflow (Step 4 intelligence + Step 5 research) and passed through
    # in the contact dict.  The script assembles them into the standard format.
    call_notes_raw = contact.get("call_notes", {})

    fields = [
        ("Why this person", call_notes_raw.get("why_this_person", "[Not provided — see Step 4/5 output]")),
        ("Process",         call_notes_raw.get("process",         "[Not provided — see Step 4 Field A]")),
        ("How Moxo Helps",  call_notes_raw.get("how_moxo_helps",  "[Not provided — see Step 4 Field D]")),
        ("Business Outcome",call_notes_raw.get("business_outcome","[Not provided — see Step 4 Field F]")),
        ("Without Moxo",    call_notes_raw.get("without_moxo",    "[Not provided — see Step 4 Field E]")),
        ("Social Proof",    call_notes_raw.get("social_proof",    "[Not provided — see moxo-kb.md]")),
    ]

    call_notes = "CALL NOTES\n\n"
    call_notes += "\n\n".join(f"{label}: {value}" for label, value in fields)

    return {
        "phone":      phone,
        "call_notes": call_notes,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cadence builder
# ─────────────────────────────────────────────────────────────────────────────

def build_cadence(contact: dict, start_dt: date) -> list[dict]:
    has_phone = bool(
        (contact.get("phone") or "").strip() or
        (contact.get("mobile") or "").strip()
    )
    blueprint = CADENCE_FULL if has_phone else CADENCE_EMAIL_ONLY

    email_map = {e["step"]: e for e in contact.get("emails", [])}
    steps = []

    for day_offset, channel, action_key, email_step in blueprint:
        step_date = (start_dt + timedelta(days=day_offset - 1)).isoformat()
        step: dict = {
            "day":        day_offset,
            "date":       step_date,
            "channel":    channel,
            "action_key": action_key,
            "action":     ACTION_LABELS.get(action_key, action_key),
        }

        if channel == "email" and email_step:
            email = email_map.get(email_step, {})
            step["subject"] = email.get("subject", f"[Missing — Step {email_step} not generated]")
            step["body"]    = email.get("body",    f"[Missing — Step {email_step} not generated]")

        elif channel == "call":
            call_data = build_call_notes(contact)
            step["phone"]      = call_data["phone"]
            step["call_notes"] = call_data["call_notes"]

        steps.append(step)

    return steps


# ─────────────────────────────────────────────────────────────────────────────
# HubSpot contact payload builder
# ─────────────────────────────────────────────────────────────────────────────

def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (full_name, "")


def build_hubspot_contact(contact: dict, rep_name: str, start_date: str) -> dict:
    first, last = _split_name(contact["contact_name"])
    phone  = (contact.get("phone")  or "").strip()
    mobile = (contact.get("mobile") or "").strip()
    notes  = contact.get("research_summary", "")
    trigger= contact.get("trigger_event", "")

    note_body = f"[Moxo BDR Research — {start_date}]\n"
    if trigger:
        note_body += f"Trigger: {trigger}\n"
    if notes:
        note_body += f"Summary: {notes}\n"
    note_body += f"Outreach rep: {rep_name}"

    props = {
        "firstname":             first,
        "lastname":              last,
        "email":                 contact.get("email") or "",
        "phone":                 phone,
        "mobilephone":           mobile,
        "company":               contact["company"],
        "jobtitle":              contact.get("contact_title", ""),
        "linkedinbio":           contact.get("linkedin") or "",
        "country":               contact.get("country", "United States"),
        "state":                 contact.get("state", ""),
        "lifecyclestage":        "lead",
        "hs_lead_status":        "NEW",
        "industry":              contact.get("industry", ""),
        # Custom Moxo properties (create if they don't exist in HubSpot)
        "moxo_sequence_name":        f"Moxo BDR — {contact.get('industry', 'Outreach')}",
        "moxo_sequence_start_date":  start_date,
        "moxo_outreach_rep":         rep_name,
        "moxo_trigger_event":        trigger,
        "notes_last_contacted":      note_body,
    }
    # Drop empty strings for cleaner HubSpot import
    props = {k: v for k, v in props.items() if v}

    # Add owner ID if provided
    owner_id = contact.get("hubspot_owner_id")
    if owner_id:
        props["hubspot_owner_id"] = owner_id

    return {
        "contact_name": contact["contact_name"],
        "company":      contact["company"],
        "email":        contact.get("email") or "",
        "properties":   props,
    }


def build_hubspot_tasks(contact: dict, cadence: list[dict]) -> list[dict]:
    """Build HubSpot task objects for all steps (email + call).

    Email steps produce individual EMAIL tasks — one task per email, not a
    single enrollment summary.  Each email task carries:
      - ``hs_task_subject``: the actual email subject line (goes into the
        subject field of the HubSpot task, NOT the notes body)
      - ``body`` / ``hs_task_body``: the email body text ONLY — no
        "Subject:" prefix, no cadence instructions, no wrapper text
    This ensures reps open each task and see a ready-to-send email with
    subject and body already in their respective fields.

    Call tasks carry the structured CALL NOTES block in the body.
    """
    tasks = []
    contact_email = contact.get("email") or ""
    company       = contact["company"]
    name          = contact["contact_name"]
    ctitle        = contact.get("contact_title", "")

    email_total = sum(1 for s in cadence if s["channel"] == "email")
    call_total  = sum(1 for s in cadence if s["channel"] == "call")
    email_idx   = 0
    call_idx    = 0

    for step in cadence:
        channel = step["channel"]

        if channel == "email":
            email_idx += 1
            email_subject = step.get("subject", f"[Email {email_idx} subject missing]")
            email_body    = step.get("body",    f"[Email {email_idx} body missing]")
            task: dict = {
                "contact_email":   contact_email,
                "company":         company,
                "due_date":        step["date"],
                "channel":         "email",
                "type":            "EMAIL",
                "action":          step["action"],
                # HubSpot task title — shows in the queue so reps know which step this is
                "subject":         f"[Email {email_idx} of {email_total}] {company} — {name} | {ctitle}",
                # hs_task_subject: the email subject line — goes into HubSpot's
                # subject field so it populates the email compose window directly
                "hs_task_subject": email_subject,
                # body / hs_task_body: email body ONLY — no "Subject:" prefix.
                # Reps see this as the email body, not as a note.
                "body":            email_body,
            }

        elif channel == "call":
            call_idx += 1
            task = {
                "contact_email": contact_email,
                "company":       company,
                "due_date":      step["date"],
                "channel":       "call",
                "type":          "CALL",
                "action":        step["action"],
                "subject":       f"[Call {call_idx} of {call_total}] {company} — {name} | {ctitle}",
                "body":          step.get("call_notes", ""),
                "phone":         step.get("phone", ""),
            }

        else:
            continue  # unknown channel — skip

        tasks.append(task)

    return tasks


# ─────────────────────────────────────────────────────────────────────────────
# CSV export for HubSpot bulk import
# ─────────────────────────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "First Name", "Last Name", "Email", "Phone Number", "Mobile Phone Number",
    "Company Name", "Job Title", "LinkedIn URL", "Country/Region", "State/Region",
    "Lifecycle Stage", "Lead Status", "Industry",
    "Moxo Sequence Name", "Moxo Sequence Start Date", "Moxo Outreach Rep",
    "Moxo Trigger Event", "Notes",
]


def contacts_to_csv_rows(contacts_out: list[dict]) -> list[dict]:
    rows = []
    for co in contacts_out:
        p = co["properties"]
        rows.append({
            "First Name":               p.get("firstname", ""),
            "Last Name":                p.get("lastname", ""),
            "Email":                    p.get("email", ""),
            "Phone Number":             p.get("phone", ""),
            "Mobile Phone Number":      p.get("mobilephone", ""),
            "Company Name":             p.get("company", ""),
            "Job Title":                p.get("jobtitle", ""),
            "LinkedIn URL":             p.get("linkedinbio", ""),
            "Country/Region":           p.get("country", ""),
            "State/Region":             p.get("state", ""),
            "Lifecycle Stage":          p.get("lifecyclestage", "lead"),
            "Lead Status":              p.get("hs_lead_status", "NEW"),
            "Industry":                 p.get("industry", ""),
            "Moxo Sequence Name":       p.get("moxo_sequence_name", ""),
            "Moxo Sequence Start Date": p.get("moxo_sequence_start_date", ""),
            "Moxo Outreach Rep":        p.get("moxo_outreach_rep", ""),
            "Moxo Trigger Event":       p.get("moxo_trigger_event", ""),
            "Notes":                    p.get("notes_last_contacted", ""),
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Main export runner
# ─────────────────────────────────────────────────────────────────────────────

def run_export(data: dict, output_dir: str) -> dict:
    rep_name   = data.get("rep_name", "Unknown Rep")
    start_str  = data.get("start_date") or date.today().isoformat()
    try:
        start_dt = date.fromisoformat(start_str)
    except ValueError:
        start_dt = date.today()

    sequences          = []
    hubspot_contacts   = []
    hubspot_tasks_all  = []
    email_only_count   = 0
    full_count         = 0

    for contact in data.get("contacts", []):
        has_phone = bool(
            (contact.get("phone")  or "").strip() or
            (contact.get("mobile") or "").strip()
        )
        if has_phone:
            full_count += 1
        else:
            email_only_count += 1

        cadence    = build_cadence(contact, start_dt)
        hs_contact = build_hubspot_contact(contact, rep_name, start_str)
        hs_tasks   = build_hubspot_tasks(contact, cadence)

        sequences.append({
            "company":        contact["company"],
            "contact_name":   contact["contact_name"],
            "contact_title":  contact.get("contact_title", ""),
            "email":          contact.get("email") or "",
            "phone":          contact.get("phone") or contact.get("mobile") or "",
            "linkedin":       contact.get("linkedin") or "",
            "cadence_type":   "FULL" if has_phone else "EMAIL_ONLY",
            "total_touches":  len(cadence),
            "sequence_days":  17,
            "start_date":     start_str,
            "end_date":       (start_dt + timedelta(days=16)).isoformat(),
            "cadence":        cadence,
        })
        hubspot_contacts.append(hs_contact)
        hubspot_tasks_all.extend(hs_tasks)

    total = len(sequences)

    enrollment_output = {
        "meta": {
            "rep_name":           rep_name,
            "export_date":        start_str,
            "total_contacts":     total,
            "full_cadence":       full_count,
            "email_only_cadence": email_only_count,
        },
        "sequences": sequences,
    }

    tasks_output = {
        "meta": {
            "rep_name":    rep_name,
            "export_date": start_str,
            "total_tasks": len(hubspot_tasks_all),
        },
        "tasks": hubspot_tasks_all,
    }

    # Write files
    os.makedirs(output_dir, exist_ok=True)

    enrollment_path = os.path.join(output_dir, "sequence_enrollment.json")
    with open(enrollment_path, "w", encoding="utf-8") as f:
        json.dump(enrollment_output, f, indent=2, ensure_ascii=False)

    tasks_path = os.path.join(output_dir, "hubspot_tasks.json")
    with open(tasks_path, "w", encoding="utf-8") as f:
        json.dump(tasks_output, f, indent=2, ensure_ascii=False)

    csv_path = os.path.join(output_dir, "hubspot_import.csv")
    csv_rows = contacts_to_csv_rows(hubspot_contacts)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(csv_rows)

    return {
        "summary": {
            "rep_name":           rep_name,
            "export_date":        start_str,
            "total_contacts":     total,
            "full_cadence":       full_count,
            "email_only_cadence": email_only_count,
            "total_tasks_queued": len(hubspot_tasks_all),
        },
        "files": {
            "sequence_enrollment": enrollment_path,
            "hubspot_tasks":       tasks_path,
            "hubspot_import_csv":  csv_path,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard formatter
# ─────────────────────────────────────────────────────────────────────────────

CHANNEL_ICONS = {"email": "✉", "call": "☎"}


def format_dashboard(result: dict, sequences: list[dict]) -> str:
    W   = 58
    sep = "═" * W
    div = "─" * W
    s   = result["summary"]

    lines = [
        "",
        sep,
        f"  Sequence Export — {s['rep_name']}",
        f"  Export date: {s['export_date']}",
        sep,
        f"  Contacts exported              : {s['total_contacts']}",
        f"  Full cadence (email + phone)   : {s['full_cadence']}",
        f"  Email-only cadence             : {s['email_only_cadence']}",
        f"  Non-email tasks queued         : {s['total_tasks_queued']}",
        div,
        "  Files written:",
        f"    sequence_enrollment.json",
        f"    hubspot_import.csv",
        f"    hubspot_tasks.json",
        sep,
        "",
        "  Per-contact cadence preview:",
        "",
    ]

    for seq in sequences:
        has_phone  = seq["cadence_type"] == "FULL"
        cadence_label = "7-touch / 17-day" if has_phone else "4-touch / 17-day"
        lines.append(f"  {seq['contact_name']}  •  {seq['company']}  •  {seq['contact_title']}")
        lines.append(f"  {seq['email'] or '[no email]'}  {'/ ' + seq['phone'] if seq['phone'] else ''}")
        lines.append(f"  Cadence: {cadence_label} ({seq['cadence_type']})")
        lines.append("")

        for step in seq["cadence"]:
            icon  = CHANNEL_ICONS.get(step["channel"], "?")
            lines.append(f"    Day {step['day']:>2}  {step['date']}  [{icon}]  {step['action']}")
            if step["channel"] == "email":
                subj = step.get("subject", "")
                lines.append(f"           Subject: {subj[:50]}{'...' if len(subj) > 50 else ''}")
            elif step["channel"] == "call":
                lines.append(f"           {seq['phone'] or '[phone needed]'}  —  Call Notes attached")

        lines += ["", div, ""]

    lines += [sep, ""]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    input_path = sys.argv[1]
    output_dir = sys.argv[2]
    quiet      = "--quiet" in sys.argv

    if not os.path.exists(input_path):
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = run_export(data, output_dir)

    if not quiet:
        # Re-load sequences for display
        enrollment_path = result["files"]["sequence_enrollment"]
        with open(enrollment_path, "r", encoding="utf-8") as f:
            enrollment = json.load(f)
        print(format_dashboard(result, enrollment["sequences"]), file=sys.stderr)

    print(json.dumps(result, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
