"""
Sends SMS via GHL API using the bot's dedicated phone number.
"""

import requests


def send_sms(ghl_api_key, location_id, to_number, from_number, message):
    """Send an SMS from the bot number to the client (e.g. Pietro)."""
    headers = {
        "Authorization": f"Bearer {ghl_api_key}",
        "Content-Type": "application/json",
        "Version": "2021-07-28",
    }
    payload = {
        "type": "SMS",
        "message": message,
        "fromNumber": from_number,
        "toNumber": to_number,
        "locationId": location_id,
    }
    resp = requests.post(
        "https://services.leadconnectorhq.com/conversations/messages",
        headers=headers,
        json=payload,
        timeout=10,
    )
    if resp.ok:
        print(f"  ✓ SMS sent to {to_number}")
    else:
        print(f"  ✗ SMS failed: {resp.status_code} {resp.text[:200]}")
    return resp.ok
