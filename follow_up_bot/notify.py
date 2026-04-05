"""
Email notifications for Evoweb — sends alerts to Mitchell.
Uses SendGrid for reliable delivery.
"""

import os
import requests

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "mitch@evoweb.com.au")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "bot@evoweb.com.au")


def send_email(subject, body):
    """Send an email notification to Mitchell."""
    if not SENDGRID_API_KEY:
        print(f"  ⚠ Email skipped (no SENDGRID_API_KEY): {subject}")
        return

    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [{"to": [{"email": NOTIFY_EMAIL}]}],
            "from": {"email": FROM_EMAIL, "name": "Evoweb Bot"},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        },
        timeout=10,
    )
    if resp.ok:
        print(f"  ✓ Email sent: {subject}")
    else:
        print(f"  ✗ Email failed: {resp.status_code} {resp.text[:200]}")


def notify_no_show(client_name, owner_name, lead_name):
    send_email(
        subject=f"No Show Alert — {client_name}",
        body=(
            f"Hi Mitch,\n\n"
            f"{owner_name}'s lead {lead_name} didn't show for their call.\n\n"
            f"{owner_name} has been asked if they want to reschedule.\n\n"
            f"Keep an eye on this one and follow up with {owner_name} if needed.\n\n"
            f"— Evoweb Bot"
        )
    )


def notify_job_won(client_name, owner_name, lead_name, amount=None):
    amount_str = f" (${amount:,.0f})" if amount else ""
    send_email(
        subject=f"Job Won{amount_str} — {client_name}",
        body=(
            f"Hi Mitch,\n\n"
            f"Great news! {owner_name} just closed {lead_name}{amount_str}.\n\n"
            f"Deposit has been confirmed.\n\n"
            f"— Evoweb Bot"
        )
    )


def notify_job_lost(client_name, owner_name, lead_name, amount=None):
    amount_str = f" (${amount:,.0f} quote)" if amount else ""
    send_email(
        subject=f"Job Lost{amount_str} — {client_name}",
        body=(
            f"Hi Mitch,\n\n"
            f"{owner_name}'s lead {lead_name}{amount_str} has been marked as lost after the full follow-up sequence.\n\n"
            f"— Evoweb Bot"
        )
    )
