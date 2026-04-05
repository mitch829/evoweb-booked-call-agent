"""
GHL API helper — opportunities, contacts, SMS.
All business data lives in GHL. This is the single source of truth.
"""

import requests

BASE_URL = "https://services.leadconnectorhq.com"


def _headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Version": "2021-07-28",
    }


# ---------------------------------------------------------------------------
# SMS
# ---------------------------------------------------------------------------

def send_sms(api_key, location_id, to_number, from_number, message, contact_id=None):
    payload = {
        "type": "SMS",
        "message": message,
        "fromNumber": from_number,
        "toNumber": to_number,
        "locationId": location_id,
    }
    if contact_id:
        payload["contactId"] = contact_id

    resp = requests.post(
        f"{BASE_URL}/conversations/messages",
        headers=_headers(api_key),
        json=payload,
        timeout=10,
    )
    if resp.ok:
        print(f"  ✓ SMS sent to {to_number}")
    else:
        print(f"  ✗ SMS failed: {resp.status_code} {resp.text[:200]}")
    return resp.ok


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------

def find_opportunity(api_key, location_id, contact_id):
    """Find an existing open opportunity for a contact."""
    resp = requests.get(
        f"{BASE_URL}/opportunities/search",
        headers=_headers(api_key),
        params={"location_id": location_id, "contact_id": contact_id, "limit": 5},
        timeout=10,
    )
    if resp.ok:
        opps = resp.json().get("opportunities", [])
        # Return the most recent open one
        open_opps = [o for o in opps if o.get("status") not in ("won", "lost", "abandoned")]
        return open_opps[0] if open_opps else None
    return None


def create_opportunity(api_key, location_id, pipeline_id, stage_id, contact_id,
                       contact_name, assigned_to=None):
    """Create a new opportunity in GHL pipeline."""
    payload = {
        "locationId": location_id,
        "pipelineId": pipeline_id,
        "pipelineStageId": stage_id,
        "contactId": contact_id,
        "name": contact_name,
        "status": "open",
    }
    if assigned_to:
        payload["assignedTo"] = assigned_to

    resp = requests.post(
        f"{BASE_URL}/opportunities/",
        headers=_headers(api_key),
        json=payload,
        timeout=10,
    )
    print(f"  Create opportunity response: {resp.status_code} {resp.text[:400]}")
    if resp.ok:
        opp = resp.json().get("opportunity", {})
        print(f"  ✓ Opportunity created: {opp.get('id')}")
        return opp
    else:
        print(f"  ✗ Create opportunity failed: {resp.status_code} {resp.text[:400]}")
        return None


def update_opportunity_stage(api_key, opportunity_id, stage_id, status=None):
    """Move opportunity to a new pipeline stage."""
    payload = {"pipelineStageId": stage_id}
    if status:
        payload["status"] = status  # open, won, lost, abandoned
    resp = requests.put(
        f"{BASE_URL}/opportunities/{opportunity_id}",
        headers=_headers(api_key),
        json=payload,
        timeout=10,
    )
    if resp.ok:
        print(f"  ✓ Opportunity {opportunity_id} moved to stage {stage_id}")
    else:
        print(f"  ✗ Update stage failed: {resp.status_code} {resp.text[:200]}")
    return resp.ok


def update_opportunity_fields(api_key, opportunity_id, monetary_value=None, name=None):
    """Update opportunity custom fields like quote amount."""
    payload = {}
    if monetary_value is not None:
        payload["monetaryValue"] = monetary_value
    if name:
        payload["name"] = name
    if not payload:
        return True
    resp = requests.put(
        f"{BASE_URL}/opportunities/{opportunity_id}",
        headers=_headers(api_key),
        json=payload,
        timeout=10,
    )
    return resp.ok


def get_pipeline_stages(api_key, location_id, pipeline_id):
    """Return {stage_name: stage_id} for a pipeline."""
    resp = requests.get(
        f"{BASE_URL}/opportunities/pipelines",
        headers=_headers(api_key),
        params={"locationId": location_id},
        timeout=10,
    )
    if not resp.ok:
        print(f"  [ERROR] Failed to fetch pipelines: {resp.status_code} {resp.text[:200]}")
        return {}
    pipelines = resp.json().get("pipelines", [])
    for p in pipelines:
        if p["id"] == pipeline_id:
            stages = {s["name"]: s["id"] for s in p.get("stages", [])}
            print(f"  [DEBUG] Found {len(stages)} stages: {stages}")
            return stages
    available_ids = [p["id"] for p in pipelines]
    print(f"  [ERROR] Pipeline {pipeline_id} not found. Available: {available_ids}")
    return {}


def add_contact_tag(api_key, location_id, contact_id, tag):
    """Add a tag to a contact — used to trigger GHL workflows."""
    resp = requests.post(
        f"{BASE_URL}/contacts/{contact_id}/tags",
        headers=_headers(api_key),
        json={"tags": [tag]},
        timeout=10,
    )
    if resp.ok:
        print(f"  ✓ Tag '{tag}' added to contact {contact_id}")
    else:
        print(f"  ✗ Tag failed: {resp.status_code} {resp.text[:200]}")
    return resp.ok


def remove_contact_tag(api_key, location_id, contact_id, tag):
    """Remove a tag from a contact."""
    resp = requests.delete(
        f"{BASE_URL}/contacts/{contact_id}/tags",
        headers=_headers(api_key),
        json={"tags": [tag]},
        timeout=10,
    )
    if resp.ok:
        print(f"  ✓ Tag '{tag}' removed from contact {contact_id}")
    else:
        print(f"  ✗ Remove tag failed: {resp.status_code} {resp.text[:200]}")
    return resp.ok


def get_contact_conversation(api_key, location_id, contact_id):
    """Fetch the most recent conversation ID for a contact."""
    resp = requests.get(
        f"{BASE_URL}/conversations/search",
        headers=_headers(api_key),
        params={"locationId": location_id, "contactId": contact_id, "limit": 1},
        timeout=10,
    )
    if resp.ok:
        convos = resp.json().get("conversations", [])
        return convos[0]["id"] if convos else None
    return None


def get_conversation_messages(api_key, conversation_id, limit=50):
    """Fetch messages from a conversation."""
    resp = requests.get(
        f"{BASE_URL}/conversations/{conversation_id}/messages",
        headers=_headers(api_key),
        params={"limit": limit},
        timeout=10,
    )
    if resp.ok:
        return resp.json().get("messages", {}).get("messages", [])
    return []


def get_contact_notes(api_key, contact_id):
    """Fetch all notes for a contact."""
    resp = requests.get(
        f"{BASE_URL}/contacts/{contact_id}/notes",
        headers=_headers(api_key),
        timeout=10,
    )
    if resp.ok:
        return [n.get("body", "") for n in resp.json().get("notes", [])]
    return []


def add_opportunity_note(api_key, contact_id, note_text):
    """Add a note to the contact (visible in GHL timeline)."""
    resp = requests.post(
        f"{BASE_URL}/contacts/{contact_id}/notes",
        headers=_headers(api_key),
        json={"body": note_text},
        timeout=10,
    )
    return resp.ok


def update_contact_custom_fields(api_key, contact_id, custom_fields):
    """Update contact custom fields. custom_fields is a dict of field_key: value."""
    if not custom_fields:
        return True
    resp = requests.put(
        f"{BASE_URL}/contacts/{contact_id}",
        headers=_headers(api_key),
        json={"customFields": custom_fields},
        timeout=10,
    )
    if resp.ok:
        print(f"  ✓ Custom fields updated for contact {contact_id}")
    else:
        print(f"  ✗ Custom field update failed: {resp.status_code} {resp.text[:200]}")
    return resp.ok
