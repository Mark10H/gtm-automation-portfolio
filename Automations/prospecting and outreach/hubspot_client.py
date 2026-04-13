"""
hubspot_client.py
-----------------
Wrapper around the HubSpot MCP integration.
Handles account pulls, contact lookups, task creation, note writing,
and enrichment flag updates.

All HubSpot custom property field names are mapped here — do not hardcode
them elsewhere in the pipeline.

Config (from config.json):
  hubspot_mcp_endpoint: str   # HubSpot MCP server URL
  rep_email_domain: str       # e.g. "moxo.com" for constructing rep emails
"""

import json
from typing import Optional


# ── Custom Property Map ────────────────────────────────────────────────────────
# Maps human-readable names to HubSpot internal property names.
# Update here if HubSpot field names change — nowhere else.

COMPANY_PROPERTIES = {
    "company_name": "name",
    "company_owner": "hubspot_owner_id",
    "number_of_employees": "numberofemployees",
    "industry": "industry",
    "country": "country",
    "domain": "domain",
    "is_active_customer": "is_an_active_customer",       # custom: dropdown Yes/No
    "ai_claude_enriched": "ai_claude_enriched",           # custom: dropdown Yes/No
    "associated_open_deals": "num_associated_deals",
}

CONTACT_PROPERTIES = {
    "first_name": "firstname",
    "last_name": "lastname",
    "email": "email",
    "phone": "phone",
    "title": "jobtitle",
    "linkedin_url": "hs_linkedin_url",
    "claude_enriched_contact": "claude_enriched_contact",  # custom: single checkbox
}

TASK_PROPERTIES = {
    "subject": "hs_task_subject",
    "body": "hs_task_body",
    "due_date": "hs_timestamp",
    "task_type": "hs_task_type",       # EMAIL or CALL
    "status": "hs_task_status",
    "assigned_to": "hubspot_owner_id",
}


# ── HubSpot Client ─────────────────────────────────────────────────────────────

class HubSpotClient:
    """
    Thin wrapper around HubSpot MCP calls.
    In production this delegates to the Claude MCP tool use interface.
    For local testing, methods can be swapped with direct HubSpot API calls.
    """

    def __init__(self, config: dict):
        self.endpoint = config.get("hubspot_mcp_endpoint")
        self.rep_email_domain = config.get("rep_email_domain", "moxo.com")

    # ── Owner / Rep Resolution ─────────────────────────────────────────────────

    def resolve_rep_owner_id(self, rep_full_name: str) -> Optional[str]:
        """
        Constructs the rep's Moxo email from their name and looks up their
        HubSpot owner ID. Returns the owner ID string or None if not found.

        Example: "Morgan Walker" -> "morgan.walker@moxo.com" -> owner ID
        """
        parts = rep_full_name.strip().lower().split()
        if len(parts) < 2:
            raise ValueError(f"Need full name (first + last), got: {rep_full_name}")

        email = f"{parts[0]}.{parts[-1]}@{self.rep_email_domain}"

        # MCP call: search_owners by email
        # owners = mcp.search_owners(email=email)
        # owner = next((o for o in owners if o["email"] == email), None)
        # return owner["id"] if owner else None

        # Placeholder for MCP call
        print(f"[HubSpot] Resolving owner ID for: {email}")
        return None  # Replace with actual MCP call

    # ── Company Pulls ──────────────────────────────────────────────────────────

    def pull_accounts_by_owner(self, owner_id: str, also_owner_b: bool = False) -> list:
        """
        Pulls all company records assigned to the rep (by HubSpot owner ID).
        If also_owner_b=True, also pulls records where Company Owner B = rep.
        Deduplicates by company ID before returning.

        Returns list of company dicts with keys from COMPANY_PROPERTIES map.
        """
        properties = list(COMPANY_PROPERTIES.values())

        # MCP call: search_crm_objects companies where owner = owner_id
        # results_a = mcp.search_crm_objects("companies", filters=[
        #     {"property": "hubspot_owner_id", "operator": "EQ", "value": owner_id}
        # ], properties=properties)

        # If dual owner rep:
        # results_b = mcp.search_crm_objects("companies", filters=[
        #     {"property": "company_owner_b", "operator": "EQ", "value": owner_id}
        # ], properties=properties)

        # Deduplicate by company ID
        # combined = {r["id"]: r for r in results_a + (results_b if also_owner_b else [])}.values()
        # return list(combined)

        print(f"[HubSpot] Pulling accounts for owner ID: {owner_id}")
        return []  # Replace with actual MCP call

    def check_open_deals(self, company_id: str) -> bool:
        """
        Returns True if the company has any open deals (not Closed Won/Lost).
        """
        # MCP call: get associated deals, check stage
        # deals = mcp.get_associated_objects("companies", company_id, "deals", ["dealstage"])
        # closed = {"closedwon", "closedlost"}
        # return any(d["dealstage"] not in closed for d in deals)
        return False  # Replace with actual MCP call

    # ── Contact Operations ─────────────────────────────────────────────────────

    def find_or_create_contact(self, contact: dict) -> str:
        """
        Searches HubSpot for an existing contact by email (if available),
        or by name + company. Creates a new record if none found.
        Returns the contact's HubSpot ID.
        """
        # MCP call: search_crm_objects contacts by email or name
        # existing = mcp.search_crm_objects("contacts", filters=[
        #     {"property": "email", "operator": "EQ", "value": contact["email"]}
        # ])
        # if existing:
        #     return existing[0]["id"]

        # Create new contact
        # new_contact = mcp.manage_crm_objects("contacts", "create", {
        #     CONTACT_PROPERTIES["first_name"]: contact["first_name"],
        #     CONTACT_PROPERTIES["last_name"]: contact["last_name"],
        #     CONTACT_PROPERTIES["email"]: contact.get("email"),
        #     CONTACT_PROPERTIES["phone"]: contact.get("phone"),
        #     CONTACT_PROPERTIES["title"]: contact.get("title"),
        #     CONTACT_PROPERTIES["linkedin_url"]: contact.get("linkedin_url"),
        # })
        # return new_contact["id"]

        print(f"[HubSpot] Finding/creating contact: {contact.get('first_name')} {contact.get('last_name')}")
        return "placeholder_contact_id"

    def mark_contact_enriched(self, contact_id: str) -> bool:
        """
        Sets the claude_enriched_contact flag on the contact record.
        Returns True on success, False on failure (failure is logged but non-blocking).
        """
        # MCP call: manage_crm_objects contacts update
        # mcp.manage_crm_objects("contacts", "update", contact_id, {
        #     CONTACT_PROPERTIES["claude_enriched_contact"]: "Yes"
        # })
        print(f"[HubSpot] Marking contact {contact_id} as enriched")
        return True

    # ── Task Creation ──────────────────────────────────────────────────────────

    def create_task(self, contact_id: str, task: dict, owner_id: str) -> Optional[str]:
        """
        Creates a HubSpot task associated with a contact record.

        task dict keys:
          - subject: str           Task title
          - body: str              Full pre-drafted email or call notes
          - task_type: str         "EMAIL" or "CALL"
          - due_date_offset: int   Days from today

        Returns task ID on success, None on failure.
        """
        from datetime import datetime, timedelta
        due_date = datetime.now() + timedelta(days=task.get("due_date_offset", 1))
        due_timestamp = int(due_date.timestamp() * 1000)  # HubSpot expects ms epoch

        # MCP call: manage_crm_objects tasks create
        # result = mcp.manage_crm_objects("tasks", "create", {
        #     TASK_PROPERTIES["subject"]: task["subject"],
        #     TASK_PROPERTIES["body"]: task["body"],
        #     TASK_PROPERTIES["task_type"]: task["task_type"],
        #     TASK_PROPERTIES["due_date"]: due_timestamp,
        #     TASK_PROPERTIES["status"]: "NOT_STARTED",
        #     TASK_PROPERTIES["assigned_to"]: owner_id,
        #     "associations": [{"to": {"id": contact_id}, "types": [{"category": "HUBSPOT_DEFINED", "typeId": 204}]}]
        # })
        # return result["id"]

        print(f"[HubSpot] Creating {task['task_type']} task for contact {contact_id}: {task['subject']}")
        return "placeholder_task_id"

    # ── Company Enrichment Flag ────────────────────────────────────────────────

    def mark_company_enriched(self, company_id: str) -> bool:
        """
        Sets the AI (Claude) Enriched? flag on the company record.
        Prevents re-processing in future batches.
        """
        # MCP call: manage_crm_objects companies update
        # mcp.manage_crm_objects("companies", "update", company_id, {
        #     COMPANY_PROPERTIES["ai_claude_enriched"]: "Yes"
        # })
        print(f"[HubSpot] Marking company {company_id} as enriched")
        return True

    # ── Note Writing ───────────────────────────────────────────────────────────

    def write_account_note(self, company_id: str, note_body: str) -> Optional[str]:
        """
        Writes an account intelligence note to the company record in HubSpot.
        Returns note ID on success, None on failure.
        """
        # MCP call: manage_crm_objects notes create
        # result = mcp.manage_crm_objects("notes", "create", {
        #     "hs_note_body": note_body,
        #     "associations": [{"to": {"id": company_id}, "types": [...]}]
        # })
        # return result["id"]

        print(f"[HubSpot] Writing note to company {company_id}")
        return "placeholder_note_id"
