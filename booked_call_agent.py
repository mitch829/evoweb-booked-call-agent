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

# Load client configs from invoicing agent
INVOICING_AGENT_PATH = pathlib.Path(__file__).parent.parent / "invoicing-agent" / "locations.json"
BOOKED_CALL_CONFIGS_PATH = pathlib.Path(__file__).parent / "follow_up_bot" / "clients"

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
        return resp.json().get("messages", {}).get("messages", [])
    return []


def update_contact_custom_fields(api_key, contact_id, custom_fields):
    """Update contact custom fields."""
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
        print(f"  ✗ Custom field update failed: {resp.status_code}")
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
    """Load all client locations from invoicing agent."""
    if INVOICING_AGENT_PATH.exists():
        return json.loads(INVOICING_AGENT_PATH.read_text())
    return []


def find_client_by_location(location_id):
    """Find client config by location ID."""
    locations = load_locations()
    for loc in locations:
        if loc.get("id") == location_id:
            return loc
    return None


def load_booked_call_config(client_name):
    """Load extraction config from follow_up_bot/clients folder."""
    # Try to find config file for this client
    slug = client_name.lower().replace(" ", "_")
    for pattern in [f"{slug}.json", "*.json"]:
        for config_file in BOOKED_CALL_CONFIGS_PATH.glob(pattern):
            config = json.loads(config_file.read_text())
            if config.get("client_name", "").lower() == client_name.lower():
                return config
    return None


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def extract_booked_call_data(messages, notes, fields, client_name):
    """
    Extract lead data from conversation messages and/or notes using Claude.
    Returns both formatted text and structured dict for custom fields.
    """
    # Build transcript from messages
    transcript = "\n".join(
        f"{'Lead' if m.get('direction') == 'inbound' else 'Bot'}: {m.get('body', '')}"
        for m in messages if m.get("body")
    )

    # Add notes if available
    notes_section = ""
    if notes:
        notes_text = "\n".join(f"- {note}" for note in notes if note)
        notes_section = f"\n\nContact Notes:\n{notes_text}"

    fields_list = "\n".join(f"- {f}" for f in fields)

    prompt = f"""You are reviewing a conversation and notes for a lead booking with {client_name}.

Extract the following information. If a field is not mentioned, write "Not mentioned".

Fields to extract:
{fields_list}

Conversation:
{transcript}{notes_section}

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
        data.get("contact", {}).get("firstName", "Unknown")
    )

    print(f"  Location: {location_id}")
    print(f"  Contact: {contact_name} ({contact_id})")

    # Find client by location
    client = find_client_by_location(location_id)
    if not client:
        print(f"  ✗ No client found for location: {location_id}")
        return jsonify({"status": "no matching client", "location": location_id}), 200

    print(f"  Client: {client.get('name')}")

    # Load extraction config for this client
    config = load_booked_call_config(client.get("name"))
    if not config:
        print(f"  — No extraction config found, using defaults")
        config = {}

    fields = config.get("extraction_fields", [])
    if not fields:
        print(f"  ✗ No extraction fields configured")
        return jsonify({"status": "no extraction fields", "client": client.get("name")}), 200

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

    messages = get_conversation_messages(api_key, convo_id, limit=100)
    notes = get_contact_notes(api_key, contact_id)

    if not messages and not notes:
        print(f"  — No messages or notes found")
        return jsonify({"status": "no data"}), 200

    print(f"  Found {len(messages)} messages, {len(notes)} notes")

    # Extract and format (uses both messages and notes)
    extraction = extract_booked_call_data(messages, notes, fields, client.get("name"))
    formatted_data = extraction["formatted_text"]
    custom_fields = extraction["custom_fields"]

    print(f"  Extracted data:\n{formatted_data}")

    # Map to HighLevel custom field format
    ghl_custom_fields = {}
    for field_name, value in custom_fields.items():
        if value and value.lower() != "not mentioned":
            ghl_key = f"contact.{field_name}"
            ghl_custom_fields[ghl_key] = value

    # Update custom fields
    if ghl_custom_fields:
        print(f"  Updating custom fields: {list(ghl_custom_fields.keys())}")
        update_contact_custom_fields(api_key, contact_id, ghl_custom_fields)

    # Add note
    note_text = f"Lead data extracted from booked call:\n{formatted_data}"
    add_contact_note(api_key, contact_id, note_text)

    print(f"  ✓ Extraction complete for {contact_name}")
    return jsonify({
        "status": "ok",
        "client": client.get("name"),
        "contact": contact_name,
        "fields_extracted": len(custom_fields),
    }), 200


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
