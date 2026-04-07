"""
Evoweb Booked Call Data Agent
Extracts and cleans lead data from booked call conversations.
Works across all sub-accounts — triggered by HighLevel booked call webhook.

Usage:
    python3 booked_call_agent.py

Webhook endpoint:
    POST /webhook/booked-call-extract
"""

import os
import json
import pathlib
import re
import anthropic
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Initialize client lazily to handle missing env vars at startup
_anthropic_client = None

def get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set")
        _anthropic_client = anthropic.Anthropic(api_key=api_key)
    return _anthropic_client

# Load client configs
LOCATIONS_PATH = pathlib.Path(__file__).parent / "locations.json"

# Extraction fields per client and mapping to HighLevel custom field keys
CLIENT_EXTRACTION_FIELDS = {
    "Allworks Earthworks": {
        "fields": [
            "postcode",
            "retaining wall height (metres)",
            "retaining wall length (metres)",
            "type of wall (e.g. concrete sleepers, timber, brick)",
            "site access"
        ],
        "field_mapping": {
            "postcode": {"key": "postalCode", "type": "standard"},
            "retaining wall height (metres)": {"key": "wall_height", "type": "custom"},
            "retaining wall length (metres)": {"key": "wall_length", "type": "custom"},
            "type of wall (e.g. concrete sleepers, timber, brick)": {"key": "type_of_wall", "type": "custom"},
            "site access": {"key": "site_access", "type": "custom"},
        }
    },
    "Trackload": {
        "fields": [
            "postcode",
            "project details"
        ],
        "field_mapping": {
            "postcode": {"key": "postalCode", "type": "standard"},
            "project details": {"key": "project_details", "type": "custom"},
        }
    },
    "Breenys Concreting": {
        "fields": [
            "postcode",
            "project size",
            "business name"
        ],
        "field_mapping": {
            "postcode": {"key": "postal_code", "type": "custom"},
            "project size": {"key": "project_size_sqm", "type": "custom"},
            "business name": {"key": "business_name", "type": "custom"},
        }
    },
    "Poletta Concrete Constructions": {
        "fields": [
            "postcode",
            "project size",
            "business name"
        ],
        "field_mapping": {
            "postcode": {"key": "postalCode", "type": "standard"},
            "project size": {"key": "project_size_sqm", "type": "custom"},
            "business name": {"key": "business_name", "type": "custom"},
        }
    },
    "MXJ Earthworks": {
        "fields": [
            "postcode",
            "retaining wall height (metres)",
            "retaining wall length (metres)",
            "type of wall (e.g. concrete sleepers, timber, brick)",
            "site access"
        ],
        "field_mapping": {
            "postcode": {"key": "postalCode", "type": "standard"},
            "retaining wall height (metres)": {"key": "wall_height", "type": "custom"},
            "retaining wall length (metres)": {"key": "wall_length", "type": "custom"},
            "type of wall (e.g. concrete sleepers, timber, brick)": {"key": "type_of_wall", "type": "custom"},
            "site access": {"key": "site_access", "type": "custom"},
        }
    },
    "Next Gen Concrete": {
        "fields": [
            "postcode",
            "project size",
            "business name"
        ],
        "field_mapping": {
            "postcode": {"key": "business.postalcode", "type": "custom"},
            "project size": {"key": "project_size_sqm", "type": "custom"},
            "business name": {"key": "business_name", "type": "custom"},
        }
    },
}

BASE_URL = "https://services.leadconnectorhq.com"


def _headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Version": "2021-07-28",
    }


# ---------------------------------------------------------------------------
# HighLevel API helpers
# ---------------------------------------------------------------------------

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


def get_conversation_messages(api_key, conversation_id, limit=100):
    """Fetch messages from a conversation."""
    resp = requests.get(
        f"{BASE_URL}/conversations/{conversation_id}/messages",
        headers=_headers(api_key),
        params={"limit": limit},
        timeout=10,
    )
    if resp.ok:
        data = resp.json()
        # Try different response structures
        if "messages" in data and isinstance(data["messages"], list):
            return data["messages"]
        elif "messages" in data and isinstance(data["messages"], dict):
            return data["messages"].get("messages", [])
        return []
    else:
        print(f"  ✗ Message fetch failed: {resp.status_code} - {resp.text[:200]}")
    return []


def update_contact_custom_fields(api_key, contact_id, custom_fields, standard_fields=None):
    """Update contact fields (both standard and custom).

    Args:
        custom_fields: dict of custom field key: value (e.g., {"wall_height": "2.2 metres"})
        standard_fields: dict of standard field key: value (e.g., {"postalCode": "3187"})
    """
    if not custom_fields and not standard_fields:
        return True

    # Build request body
    body = {}

    # Add standard fields as top-level properties
    if standard_fields:
        body.update(standard_fields)

    # Add custom fields in the customFields array
    if custom_fields:
        fields_array = [{"key": k, "value": v} for k, v in custom_fields.items()]
        body["customFields"] = fields_array

    resp = requests.put(
        f"{BASE_URL}/contacts/{contact_id}",
        headers=_headers(api_key),
        json=body,
        timeout=10,
    )
    if resp.ok:
        print(f"  ✓ Fields updated for contact {contact_id}")
    else:
        print(f"  ✗ Field update failed: {resp.status_code}")
        print(f"     Response: {resp.text[:300]}")
    return resp.ok


def add_contact_note(api_key, contact_id, note_text):
    """Add a note to the contact."""
    resp = requests.post(
        f"{BASE_URL}/contacts/{contact_id}/notes",
        headers=_headers(api_key),
        json={"body": note_text},
        timeout=10,
    )
    return resp.ok


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


# ---------------------------------------------------------------------------
# Client lookup
# ---------------------------------------------------------------------------

def load_locations():
    """Load all client locations."""
    if LOCATIONS_PATH.exists():
        return json.loads(LOCATIONS_PATH.read_text())
    print(f"  ⚠ locations.json not found at {LOCATIONS_PATH}")
    return []


def find_client_by_location(location_id):
    """Find client config by location ID."""
    locations = load_locations()
    for loc in locations:
        if loc.get("id") == location_id:
            return loc
    return None


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def extract_booked_call_data(messages, notes, fields, client_name):
    """
    Extract lead data from conversation messages and/or notes using Claude.
    Returns both formatted text and structured dict for custom fields.
    Prioritizes notes over other sources when both exist.
    """
    # Build transcript from messages
    transcript = "\n".join(
        f"{'Lead' if m.get('direction') == 'inbound' else 'Bot'}: {m.get('body', '')}"
        for m in messages if m.get("body")
    )

    # Add notes if available - these are priority
    notes_section = ""
    if notes:
        notes_text = "\n".join(f"- {note}" for note in notes if note)
        notes_section = f"\n\n**IMPORTANT - PRIORITY DATA (from notes):**\n{notes_text}"

    fields_list = "\n".join(f"- {f}" for f in fields)

    prompt = f"""You are reviewing a conversation and notes for a lead booking with {client_name}.

Extract the following information. **If the field appears in the notes (PRIORITY DATA), use that value. Otherwise extract from the conversation. If a field is not mentioned anywhere, write "Not mentioned".**

Fields to extract:
{fields_list}

SPECIAL FIELD INSTRUCTIONS:
- business name: Extract the name of the CLIENT/BUSINESS being quoted for (NOT the service provider). If notes say "builder for evoweb", extract "evoweb"
- site access: Extract any information about how to access the site or property

Conversation:
{transcript}{notes_section}

IMPORTANT: For fields with units (metres, sqm, etc), include the units in the value.
For example: "2.2 metres" not just "2.2"

Reply in exactly this format (one per line):
field: value"""

    response = get_anthropic_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    extracted_text = response.content[0].text.strip()

    # Format nicely for copy/paste
    lines = [f"\n=== {client_name} Lead Data ===\n"]
    lines.append(extracted_text)
    lines.append("\n=== Ready to copy/paste to WhatsApp ===\n")
    formatted_text = "\n".join(lines)

    # Parse into dict for custom fields
    custom_fields_dict = {}
    for line in extracted_text.split('\n'):
        if ':' in line:
            key, val = line.split(':', 1)
            field_name = key.strip().lower().replace(" ", "_").replace("(", "").replace(")", "")
            custom_fields_dict[field_name] = val.strip()

    return {
        "formatted_text": formatted_text,
        "custom_fields": custom_fields_dict,
    }


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@app.route("/webhook/booked-call-extract", methods=["POST"])
def webhook_booked_call_extract():
    """
    Extract lead data from booked call conversation.
    Triggered 1-5 minutes after booked call is created.
    Updates custom fields and adds note to contact.
    Works across all sub-accounts.
    """
    data = request.json or {}
    print(f"\n📞 Booked Call Extraction Webhook")
    print(f"Full raw payload: {json.dumps(data)[:500]}")

    # Try multiple ways to extract locationId (HighLevel can send it different ways)
    location_id = (
        data.get("locationId") or
        data.get("location_id") or
        data.get("location", {}).get("id", "") or
        data.get("contact", {}).get("locationId", "")
    )

    contact_id = (
        data.get("contactId") or
        data.get("contact_id") or
        data.get("contact", {}).get("id", "")
    )

    contact_name = (
        data.get("contactName") or
        data.get("contact_name") or
        data.get("full_name") or
        data.get("first_name") or
        data.get("contact", {}).get("firstName", "Unknown")
    )

    print(f"  Location: {location_id}")
    print(f"  Contact: {contact_name} ({contact_id})")

    # Find client by location
    client = find_client_by_location(location_id)
    if not client:
        print(f"  ✗ No client found for location: {location_id}")
        return jsonify({"status": "no matching client", "location": location_id}), 200

    client_name = client.get("name")
    print(f"  Client: {client_name}")

    # Get extraction fields for this client
    client_config = CLIENT_EXTRACTION_FIELDS.get(client_name)
    if not client_config:
        print(f"  ✗ No extraction config for {client_name}")
        return jsonify({"status": "no extraction fields", "client": client_name}), 200

    fields = client_config.get("fields", [])
    field_mapping = client_config.get("field_mapping", {})
    if not fields:
        print(f"  ✗ No extraction fields configured for {client_name}")
        return jsonify({"status": "no extraction fields", "client": client_name}), 200

    print(f"  Fields to extract: {fields}")

    api_key = client.get("api_key")
    if not api_key:
        print(f"  ✗ No API key for client")
        return jsonify({"status": "no api key"}), 500

    # Fetch conversation from GHL
    convo_id = get_contact_conversation(api_key, location_id, contact_id)
    if not convo_id:
        print(f"  — No conversation found")
        return jsonify({"status": "no conversation"}), 200

    print(f"  Fetching conversation for contact {contact_id}...")
    # Small delay to allow HighLevel to index messages
    import time
    time.sleep(1)
    messages = get_conversation_messages(api_key, convo_id, limit=100)
    print(f"  Got {len(messages)} messages")

    print(f"  Fetching notes for contact {contact_id}...")
    notes = get_contact_notes(api_key, contact_id)
    print(f"  Got {len(notes)} notes")
    if notes:
        print(f"  Notes content: {notes}")

    if not messages and not notes:
        print(f"  ✗ No messages or notes found")
        return jsonify({"status": "no data"}), 200

    print(f"  ✓ Found {len(messages)} messages, {len(notes)} notes")

    # Extract and format (uses both messages and notes)
    print(f"  Extracting with fields: {fields}")
    extraction = extract_booked_call_data(messages, notes, fields, client_name)
    formatted_data = extraction["formatted_text"]
    custom_fields = extraction["custom_fields"]

    print(f"  ✓ Extraction complete. Raw extracted fields:")
    for k, v in custom_fields.items():
        print(f"    {k}: {v}")

    # Map extracted data to HighLevel field format using client's field mapping
    ghl_standard_fields = {}
    ghl_custom_fields = {}
    for orig_field_name, field_config in field_mapping.items():
        # Handle both old string format and new dict format for backwards compatibility
        if isinstance(field_config, str):
            ghl_key = field_config
            field_type = "custom"
        else:
            ghl_key = field_config.get("key")
            field_type = field_config.get("type", "custom")

        # Try multiple ways to find the matching extracted field
        snake_key1 = orig_field_name.lower().replace(" ", "_").replace("(", "").replace(")", "").replace(".", "")
        snake_key2 = orig_field_name.lower().split("(")[0].strip().replace(" ", "_")  # Just the first part

        value = None
        for key in [snake_key1, snake_key2]:
            if key in custom_fields:
                value = custom_fields[key]
                break

        if value and value.lower() != "not mentioned":
            if field_type == "standard":
                ghl_standard_fields[ghl_key] = value
                print(f"    → {orig_field_name} -> {ghl_key} (standard) = {value}")
            else:
                ghl_custom_fields[ghl_key] = value
                print(f"    → {orig_field_name} -> {ghl_key} (custom) = {value}")
        else:
            print(f"    ✗ {orig_field_name} not found in extracted data")

    # Update fields (both standard and custom)
    total_fields = len(ghl_custom_fields) + len(ghl_standard_fields)
    if total_fields > 0:
        print(f"  Updating {total_fields} fields in GHL...")
        success = update_contact_custom_fields(api_key, contact_id, ghl_custom_fields, ghl_standard_fields)
        if success:
            print(f"  ✓ Custom fields updated")
        else:
            print(f"  ✗ Custom field update failed")
    else:
        print(f"  — No fields to update (all were 'not mentioned')")

    # Add note
    print(f"  Adding note to contact...")
    note_text = f"Lead data extracted from booked call:\n{formatted_data}"
    note_success = add_contact_note(api_key, contact_id, note_text)
    if note_success:
        print(f"  ✓ Note added")
    else:
        print(f"  ✗ Note add failed")

    # Send email notification with extracted data
    print(f"  Sending booked call email...")
    send_booked_call_email(client_name, contact_name, formatted_data)

    print(f"  ✓ Extraction complete for {contact_name}")
    return jsonify({
        "status": "ok",
        "client": client_name,
        "contact": contact_name,
        "fields_extracted": len(custom_fields),
    }), 200


@app.route("/webhook/booked-call-extract-debug", methods=["POST"])
def webhook_debug():
    """Debug endpoint — just logs and returns the raw payload."""
    data = request.json or {}
    print(f"\n🔍 DEBUG WEBHOOK PAYLOAD:")
    print(json.dumps(data, indent=2)[:1000])
    return jsonify({"debug": "payload logged", "received_keys": list(data.keys())}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "evoweb-booked-call-agent"}), 200


if __name__ == "__main__":
    print("Evoweb Booked Call Data Agent")
    print("=" * 50)
    print("Webhook: POST /webhook/booked-call-extract")
    print("=" * 50)
    port = int(os.environ.get("PORT", 8082))
    app.run(host="0.0.0.0", port=port, debug=False)
