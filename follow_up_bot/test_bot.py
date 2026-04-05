"""
Local test script for the Follow-Up Bot.
Simulates a new appointment webhook and SMS replies without needing GHL.

Usage:
    # Terminal 1 — start the bot
    python3 bot.py

    # Terminal 2 — run the tests
    python3 test_bot.py
"""

import requests
import time
import json

BASE_URL = "http://localhost:8081"


def print_section(title):
    print(f"\n{'='*50}")
    print(f"  {title}")
    print(f"{'='*50}")


def create_test_contact():
    """Create a real contact in GHL for testing."""
    import json
    config = json.load(open("clients/dummy.json"))
    headers = {
        "Authorization": f"Bearer {config['ghl_api_key']}",
        "Content-Type": "application/json",
        "Version": "2021-07-28",
    }
    payload = {
        "locationId": config["ghl_location_id"],
        "firstName": "Bob",
        "lastName": "Smith",
        "phone": "+61412345678",
        "email": "bob.smith.test@example.com",
        "companyName": "Smith Concreting",
        "city": "Ringwood",
        "tags": ["test-lead"],
    }
    resp = requests.post(
        "https://services.leadconnectorhq.com/contacts/",
        headers=headers,
        json=payload,
    )
    if resp.ok:
        contact_id = resp.json().get("contact", {}).get("id")
        print(f"  ✓ Test contact created: {contact_id}")
        return contact_id, config["ghl_location_id"]
    else:
        print(f"  ✗ Failed to create contact: {resp.status_code} {resp.text[:200]}")
        return None, None


EXISTING_CONTACT_ID = "UUu1zMedXr5ojVg6CnQI"
LOCATION_ID = "7P6tHbrRBkKqFBuqv34j"

def test_appointment():
    print_section("1. Simulating appointment booked")
    payload = {
        "locationId": LOCATION_ID,
        "startTime": "2026-03-23T09:00:00+11:00",
        "contactId": EXISTING_CONTACT_ID,
        "contact": {
            "id": EXISTING_CONTACT_ID,
            "firstName": "Bob",
            "lastName": "Smith",
            "phone": "+61412345678",
            "email": "bob.smith.test@example.com",
            "companyName": "Smith Concreting",
            "city": "Ringwood",
        }
    }
    resp = requests.post(f"{BASE_URL}/webhook/appointment", json=payload)
    print(f"  Status: {resp.status_code}")
    print(f"  Response: {resp.json()}")
    return LOCATION_ID


def test_sms_reply(message, location_id="7P6tHbrRBkKqFBuqv34j"):
    print_section(f"2. Mitchell replies: '{message}'")
    payload = {
        "locationId": location_id,
        "type": "InboundMessage",
        "body": message,
        "phone": "+61434485396",
        "fromNumber": "+61434485396",
    }
    resp = requests.post(f"{BASE_URL}/webhook/sms", json=payload)
    print(f"  Status: {resp.status_code}")
    print(f"  Response: {resp.json()}")


def test_brain_only():
    """Test Claude's interpretation without running the full bot."""
    print_section("Testing Claude brain directly")
    from brain import interpret_reply, draft_followup as draft_followup_text

    fake_config = json.loads(open("clients/poletta.json").read())
    fake_lead = {
        "id": 1,
        "contact_name": "Bob Smith",
        "contact_id": "test-001",
        "opportunity_id": "opp-001",
        "suburb": "Ringwood",
        "current_stage": "POST_CALL",
        "current_question": None,
        "notes": "",
    }

    # Test message generation
    from bot import get_message
    print("\n--- Messages Pietro would receive ---")
    for state in ["POST_CALL", "SITE_VISIT", "ASK_QUOTE_AMOUNT", "QUOTE_SENT", "FOLLOWING_UP", "LAST_CHANCE"]:
        fake_lead["current_stage"] = state
        msg = get_message(state, fake_lead, fake_config)
        print(f"\n[{state}]\n{msg}")

    # Test reply interpretation
    print("\n--- Reply interpretation ---")
    test_replies = [
        ("POST_CALL", "1"),
        ("POST_CALL", "yeah good call, sending quote tomorrow"),
        ("QUOTING", "sent"),
        ("QUOTE_SENT", "not yet still chasing"),
        ("FOLLOWING_UP", "2"),
        ("FOLLOWING_UP", "he went elsewhere"),
    ]
    for state, reply in test_replies:
        fake_lead["current_stage"] = state
        result = interpret_reply(reply, state, fake_lead, fake_config)
        print(f"\n  State: {state} | Reply: '{reply}'")
        print(f"  → {result}")

    # Test follow-up draft
    print("\n--- Follow-up text draft ---")
    draft = draft_followup_text(fake_lead, fake_config)
    print(f"  Draft: \"{draft}\"")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "brain":
        # Test Claude logic only — no bot server needed
        test_brain_only()
    else:
        # Test full webhook flow — requires bot.py running in another terminal
        print("Make sure bot.py is running in another terminal first.")
        print("Testing full webhook flow...\n")

        # Check bot is running
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except Exception:
            print("ERROR: Bot is not running. Start it with: python3 bot.py")
            exit(1)

        # Step 1: New appointment + create contact
        location_id = test_appointment()
        time.sleep(2)

        # Force fire the first message immediately (scheduler runs every 5 min)
        requests.get(f"{BASE_URL}/debug/fire")
        time.sleep(2)

        # Step 2: Reply — call went well, sending quote
        test_sms_reply("1", location_id)
        time.sleep(2)

        # Step 3: Reply — quote amount
        test_sms_reply("4500", location_id)
        time.sleep(1)
        time.sleep(1)

        print("\n✅ Test complete. Check bot.py terminal for logs.")
        print("\nTo test Claude logic only (no bot needed):")
        print("  python3 test_bot.py brain")
