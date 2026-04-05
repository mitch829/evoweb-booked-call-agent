"""
Evoweb Follow-Up Bot
====================
Handles the full lead follow-up sequence via SMS for Evoweb clients.

- All business data lives in GHL (opportunities, stages, quote amounts)
- Queue DB only tracks bot conversation state
- One message at a time per client — no doubling up
- Claude interprets natural language replies

Usage:
    python3 bot.py

Webhooks to configure in GHL:
    POST /webhook/appointment  — fires when appointment is created/confirmed
    POST /webhook/sms          — fires when Pietro replies via SMS
"""

import os
import json
import pathlib
import datetime
import pytz
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

import ghl
import brain
import notify
from lead_queue import (add_to_queue, get_waiting, get_next_due,
                        update_queue, set_next_action, set_nudge,
                        get_due_nudges, pause_all, resume_all)

load_dotenv()

app = Flask(__name__)
scheduler = BackgroundScheduler()
CLIENTS_DIR = pathlib.Path(__file__).parent / "clients"

# Deduplication: track recently processed webhook message IDs to prevent double-processing
# GHL occasionally retries webhooks — this prevents duplicate stage transitions
_processed_message_ids: set = set()
_MAX_DEDUP_SIZE = 500  # cap memory usage

# ---------------------------------------------------------------------------
# Client config helpers
# ---------------------------------------------------------------------------

def load_client(client_id):
    path = CLIENTS_DIR / f"{client_id}.json"
    if not path.exists():
        return None
    config = json.loads(path.read_text())
    config["_client_id"] = client_id
    return config


def find_client_by_location(location_id):
    for f in CLIENTS_DIR.glob("*.json"):
        c = json.loads(f.read_text())
        if c.get("ghl_location_id") == location_id:
            c["_client_id"] = f.stem
            return f.stem, c
    return None, None


def find_client_by_owner_number(from_number):
    clean = from_number.replace(" ", "").replace("+", "").lstrip("0")
    for f in CLIENTS_DIR.glob("*.json"):
        c = json.loads(f.read_text())
        owner = c.get("owner_mobile", "").replace(" ", "").replace("+", "").lstrip("0")
        if owner and (owner in clean or clean in owner):
            c["_client_id"] = f.stem
            return f.stem, c
    return None, None


def get_stages(config):
    """Return stage map from config (hardcoded to avoid API issues)."""
    stages = config.get("stage_ids", {})
    if stages:
        return stages
    # Fallback to API lookup if not in config
    return ghl.get_pipeline_stages(
        config["ghl_api_key"],
        config["ghl_location_id"],
        config["pipeline_id"],
    )


def send(config, message, contact_id=None):
    """Send SMS from bot number to owner. Returns True if sent, False if failed."""
    return ghl.send_sms(
        api_key=config["ghl_api_key"],
        location_id=config["ghl_location_id"],
        to_number=config["owner_mobile"],
        from_number=config["bot_phone_number"],
        message=message,
        contact_id=config.get("owner_contact_id"),
    )


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def get_message(stage, lead, config):
    owner = config["owner_name"]
    name = lead["contact_name"].split()[0] if lead.get("contact_name") else "them"
    full_name = lead.get("contact_name", "")
    suburb = lead.get("suburb", "")
    sp = config["sales_process"]

    context = full_name
    if suburb:
        context += f" ({suburb})"

    messages = {
        "POST_CALL": (
            f"Hey {owner}, how did the call go with {context}?\n"
            f"Reply:\n1 = Preparing quote\n2 = Need site visit first\n3 = No show\n4 = Not interested"
        ),
        "SITE_VISIT": (
            f"Hey {owner}, have you done the site visit with {name} yet?\n"
            f"Reply:\nDONE = Yes, in quote\nNOT YET = Haven't done it yet\nLOST = Not going ahead"
        ),
        "ASK_QUOTE_AMOUNT": (
            f"How much is the quote for {name}? Reply with the amount e.g. $20,000"
        ),
        "QUOTE_CONFIRM": (
            f"Hey {owner}, have you sent {name} the quote yet?\n"
            f"Reply:\nYES = Sent it\nNOT YET = Still working on it\nLOST = Not going ahead"
        ),
        "QUOTE_SENT": (
            f"Hey {owner}, it's been {sp.get('quote_followup_days', 3)} days since you sent {name} the quote. "
            f"Heard back?\nReply:\n1 = He's keen\n2 = Still chasing\n3 = Lost him"
        ),
        "DEPOSIT_PENDING": (
            f"Hey {owner}, has {name} paid the deposit yet?\n"
            f"Reply:\nYES = Deposit paid\nNOT YET = Still waiting\nLOST = Not going ahead"
        ),
        "FOLLOW_1": (
            f"Hey {owner}, just checking in on {name} — any word back on the quote?\n"
            f"Reply:\n1 = He's in\n2 = Still chasing\n3 = Write him off"
        ),
        "FOLLOW_2": (
            f"Hey {owner}, following up on {name} again. Sometimes a quick call nudges them. Worth a try?\n"
            f"Reply:\n1 = He's in\n2 = Will give him a call\n3 = Write him off"
        ),
        "FOLLOW_3": (
            f"Hey {owner}, {name} has gone quiet for a bit now. Have you tried a different angle — maybe adjusted the price or scope?\n"
            f"Reply:\n1 = He's in\n2 = Will try a different approach\n3 = Write him off"
        ),
        "FOLLOW_4": (
            f"Hey {owner}, still worth chasing {name}? Sometimes people just need more time.\n"
            f"Reply:\n1 = He's in\n2 = Still in the running\n3 = Write him off"
        ),
        "FOLLOW_5": (
            f"Hey {owner}, quick one on {name} — have you got any other jobs lined up with him or referrals from him worth protecting?\n"
            f"Reply:\n1 = He's in\n2 = Worth one more push\n3 = Write him off"
        ),
        "FOLLOW_6": (
            f"Hey {owner}, last warning on {name} — I'm about to close this one off. Want to make one final attempt?\n"
            f"Reply:\nTRY = Yes, one more go\nDONE = Close him off"
        ),
        "FOLLOW_7": (
            f"Hey {owner}, closing off {name} now. If he comes back, just start a new appointment and I'll pick it up.\n"
            f"Reply:\nOK = Got it"
        ),
        "NO_SHOW": (
            f"Hey {owner}, looks like {name} didn't show. Worth rescheduling?\n"
            f"Reply:\nYES = I'll reschedule\nNO = Close him off"
        ),
    }
    return messages.get(stage, f"Hey {owner}, any update on {name}?")


# ---------------------------------------------------------------------------
# Reply handler
# ---------------------------------------------------------------------------

def handle_reply(reply, lead, config):
    """
    Process owner's reply using Claude.
    Returns: {"next_stage": str, "days": int, "opp_update": dict, "follow_message": str|None}
    """
    owner = config["owner_name"]
    current = lead["current_stage"]
    name = lead["contact_name"].split()[0] if lead.get("contact_name") else "them"
    sp = config["sales_process"]
    follow_intervals = sp.get("follow_intervals", [3, 5, 7, 10, 12, 14, 21])

    result = brain.interpret_reply(reply, current, lead, config)
    next_stage = result["next_stage"]
    days = result.get("days", 3)
    note = result.get("note", "")

    opp_update = {}
    follow_message = None

    if current == "POST_CALL":
        if next_stage == "QUOTING":
            follow_message = f"No worries, I'll check back in 2 days to get the quote amount for {name}."
            next_stage = "ASK_QUOTE_AMOUNT"
            days = 2
        elif next_stage == "SITE_VISIT":
            follow_message = f"Got it — I'll follow up in 3 days to check if the site visit with {name} has been booked."
            next_stage = "SITE_VISIT"
            days = 3
        elif next_stage == "NO_SHOW":
            follow_message = f"Got it, Mitch has been notified. He'll go ahead and reschedule that call."
            next_stage = "NO_SHOW"
        elif next_stage in ("LOST", "NOT_INTERESTED", "JOB_LOST"):
            follow_message = f"Got it, {name} marked as lost. On to the next one 💪"
            opp_update["status"] = "lost"
            next_stage = "JOB_LOST"
            ghl.add_contact_tag(config["ghl_api_key"], config["ghl_location_id"], lead["contact_id"], "unqualified")

    elif current == "SITE_VISIT":
        if next_stage in ("QUOTING", "DONE"):
            follow_message = get_message("ASK_QUOTE_AMOUNT", lead, config)
            next_stage = "ASK_QUOTE_AMOUNT"
        elif next_stage == "NOT_YET":
            days = 2
            follow_message = f"No worries, I'll check back in 2 days on {name}."
            next_stage = "SITE_VISIT"
        elif next_stage in ("LOST", "JOB_LOST"):
            follow_message = f"Got it, {name} marked as lost. 💪"
            opp_update["status"] = "lost"
            next_stage = "JOB_LOST"

    elif current == "ASK_QUOTE_AMOUNT":
        amount = brain.extract_amount(reply)
        if amount:
            opp_update["monetary_value"] = amount
            confirm_days = sp.get("quote_confirm_days", 2)
            follow_message = f"Got it — ${amount:,.0f} logged for {name}. I'll check back in {confirm_days} days to see if you've sent it."
            next_stage = "QUOTE_CONFIRM"
            days = confirm_days
        else:
            # Not ready yet — check back tomorrow
            follow_message = f"No worries, I'll check back tomorrow for {name}'s quote amount."
            next_stage = "ASK_QUOTE_AMOUNT"
            days = 1

    elif current == "QUOTE_CONFIRM":
        if next_stage in ("YES", "SENT"):
            follow_message = f"Nice one. I'll check back in {sp.get('quote_followup_days', 3)} days to see how {name} responded."
            next_stage = "QUOTE_SENT"
            days = sp.get("quote_followup_days", 3)
        elif next_stage == "NOT_YET":
            follow_message = f"No worries, I'll check in on {name}'s quote tomorrow."
            days = 1
            next_stage = "QUOTE_CONFIRM"
        elif next_stage in ("LOST", "JOB_LOST"):
            follow_message = f"Got it, {name} marked as lost. 💪"
            opp_update["status"] = "lost"
            next_stage = "JOB_LOST"

    elif current == "QUOTE_SENT":
        if next_stage in ("WON", "KEEN", "JOB_WON"):
            follow_message = get_message("DEPOSIT_PENDING", lead, config)
            next_stage = "DEPOSIT_PENDING"
            days = 3
        elif next_stage in ("FOLLOWING_UP", "CHASING"):
            follow_message = f"No worries, I'll keep following up on {name}. I'll check back in {follow_intervals[0]} days."
            next_stage = "FOLLOW_1"
            days = follow_intervals[0]
        elif next_stage in ("LOST", "JOB_LOST"):
            follow_message = f"Got it, {name} marked as lost. Keep grinding 💪"
            opp_update["status"] = "lost"
            next_stage = "JOB_LOST"

    elif current == "DEPOSIT_PENDING":
        if next_stage in ("YES", "PAID", "JOB_WON"):
            follow_message = f"Let's go! {name} is locked in 🔥 Make sure the job's in the calendar."
            opp_update["status"] = "won"
            next_stage = "JOB_WON"
        elif next_stage == "NOT_YET":
            follow_message = f"No worries, I'll check back in 3 days on {name}'s deposit."
            days = 3
            next_stage = "DEPOSIT_PENDING"
        elif next_stage in ("LOST", "JOB_LOST"):
            follow_message = f"Got it, {name} marked as lost. 💪"
            opp_update["status"] = "lost"
            next_stage = "JOB_LOST"

    elif current.startswith("FOLLOW_"):
        follow_num = int(current.split("_")[1])
        if next_stage in ("WON", "KEEN", "JOB_WON"):
            follow_message = get_message("DEPOSIT_PENDING", lead, config)
            next_stage = "DEPOSIT_PENDING"
            days = 3
        elif next_stage in ("LOST", "WRITEOFF", "JOB_LOST", "DONE"):
            follow_message = f"Got it, closing {name} off. On to better leads 💪"
            opp_update["status"] = "lost"
            next_stage = "JOB_LOST"
        elif next_stage in ("TRY", "FOLLOWING_UP", "CHASING", "STILL"):
            next_follow = follow_num + 1
            if next_follow <= 7:
                next_stage = f"FOLLOW_{next_follow}"
                days = follow_intervals[next_follow - 1] if next_follow - 1 < len(follow_intervals) else 7
                follow_message = f"Got it, I'll check back in {days} days on {name}."
            else:
                # End of follow-up sequence
                follow_message = f"Got it, closing {name} off after no response. 💪"
                opp_update["status"] = "lost"
                next_stage = "JOB_LOST"

    if note:
        ghl.add_opportunity_note(config["ghl_api_key"], lead["contact_id"], note)

    # immediate_reply = True means we just asked a question and need a reply right away
    # False means we sent a confirmation and next message is scheduled for later
    immediate_reply = next_stage in ("DEPOSIT_PENDING",) or (
        next_stage == "ASK_QUOTE_AMOUNT" and current != "POST_CALL"
    )

    return {
        "next_stage": next_stage,
        "days": days,
        "opp_update": opp_update,
        "follow_message": follow_message,
        "immediate_reply": immediate_reply,
    }


# ---------------------------------------------------------------------------
# Scheduler — fires due messages every 5 minutes
# ---------------------------------------------------------------------------

def scheduler_tick():
    import sys
    import traceback
    try:
        msg = f"\n[SCHEDULER] Tick started at {datetime.datetime.now().isoformat()}"
        print(msg, flush=True)
        sys.stdout.flush()
        with open('/tmp/scheduler_debug.log', 'a') as f:
            f.write(msg + '\n')
            f.flush()
        for f in CLIENTS_DIR.glob("*.json"):
            client_id = f.stem
            config = load_client(client_id)
            if not config or not config.get("owner_mobile") or not config.get("bot_phone_number"):
                print(f"  [{client_id}] Missing config, skipping")
                continue
            if config.get("paused"):
                print(f"  [{client_id}] Paused, skipping")
                continue

            nudge_hours = config.get("nudge_after_hours", 2)
            max_nudges = config.get("max_nudges", 2)

            # Only send between 8am and 8pm in client's timezone
            tz = pytz.timezone(config.get("timezone", "Australia/Sydney"))
            now_local = datetime.datetime.now(tz)
            if not (9 <= now_local.hour < 18):
                print(f"  [{client_id}] Outside business hours ({now_local.hour}:00 {tz}), skipping")
                continue

            # Send nudges for leads that haven't replied
            for lead in get_due_nudges(client_id, max_nudges=max_nudges):
                name = lead["contact_name"].split()[0] if lead.get("contact_name") else "them"
                owner = config["owner_name"]
                nudge_msg = f"Hey {owner}, just checking — did you see my last message about {name}?"
                if send(config, nudge_msg):
                    new_count = (lead.get("nudge_count") or 0) + 1
                    update_queue(lead["id"], nudge_count=new_count, nudge_at=None)
                    # Schedule another nudge if under limit
                    if new_count < max_nudges:
                        set_nudge(lead["id"], nudge_hours)
                    print(f"  → [{client_id}] Nudge {new_count} sent for {lead['contact_name']}")
                else:
                    print(f"  ✗ [{client_id}] Nudge failed for {lead['contact_name']}, will retry next tick")

            # Send scheduled messages (only if no one is currently waiting)
            if get_waiting(client_id):
                continue

            lead = get_next_due(client_id)
            if not lead:
                continue

            stage = lead["current_stage"]
            message = get_message(stage, lead, config)
            if send(config, message, contact_id=lead.get("contact_id")):
                update_queue(lead["id"], waiting_reply=1, current_question=stage, nudge_count=0)
                set_nudge(lead["id"], nudge_hours)
                print(f"  → [{client_id}] Sent {stage} message for {lead['contact_name']}")
            else:
                print(f"  ✗ [{client_id}] Failed to send {stage} for {lead['contact_name']}, will retry")
    except Exception as e:
        err_msg = f"[SCHEDULER ERROR] {str(e)}\n{traceback.format_exc()}"
        print(err_msg, flush=True)
        with open('/tmp/scheduler_debug.log', 'a') as f:
            f.write(err_msg + '\n')
            f.flush()


scheduler.add_job(scheduler_tick, "interval", minutes=5)
scheduler.start()
print(f"[INIT] Scheduler started at {datetime.datetime.now().isoformat()}", flush=True)


# ---------------------------------------------------------------------------
# Webhook: new appointment booked
# ---------------------------------------------------------------------------

@app.route("/webhook/appointment", methods=["POST"])
def webhook_appointment():
    data = request.json or {}

    # Support both GHL payload formats
    location_id = (data.get("locationId")
                   or data.get("location", {}).get("id", ""))
    client_id, config = find_client_by_location(location_id)

    if not config:
        print(f"  — No client found for location_id: {location_id}")
        return jsonify({"status": "no matching client"}), 200

    # Skip cancelled appointments
    calendar = data.get("calendar", {})
    status = (data.get("appointmentStatus")
              or calendar.get("appoinmentStatus")
              or calendar.get("appointmentStatus")
              or "").lower()
    if status in ("cancelled", "invalid"):
        print(f"  — Appointment cancelled, skipping")
        return jsonify({"status": "cancelled"}), 200

    contact = data.get("contact", {})
    contact_id = (contact.get("id")
                  or data.get("contactId")
                  or data.get("contact_id", ""))
    first = contact.get("firstName") or data.get("first_name", "")
    last = contact.get("lastName") or data.get("last_name", "")
    contact_name = (data.get("full_name")
                    or f"{first} {last}".strip()
                    or "Unknown")
    phone = contact.get("phone") or data.get("phone", "")
    suburb = contact.get("city") or data.get("city", "")
    call_time = data.get("startTime") or calendar.get("startTime", "")

    # Check for existing open opportunity — don't duplicate
    existing_opp = ghl.find_opportunity(config["ghl_api_key"], location_id, contact_id)

    if existing_opp:
        opp_id = existing_opp["id"]
        print(f"  — Existing opportunity found ({opp_id}), reusing")
    else:
        # Create new opportunity
        stages = get_stages(config)
        stage_map = config.get("stage_map", {})
        ghl_stage_name = stage_map.get("POST_CALL")
        stage_id = stages.get(ghl_stage_name) if ghl_stage_name else (list(stages.values())[0] if stages else None)
        opp = ghl.create_opportunity(
            api_key=config["ghl_api_key"],
            location_id=location_id,
            pipeline_id=config["pipeline_id"],
            stage_id=stage_id,
            contact_id=contact_id,
            contact_name=contact_name,
        )
        opp_id = opp["id"] if opp else None

    if not opp_id:
        return jsonify({"status": "failed to get opportunity"}), 500

    # If lead was in NO_SHOW stage, remove tag and reset so they restart the flow
    from lead_queue import _get_conn, _ph
    conn, db = _get_conn()
    ph = _ph(db)
    cur = conn.cursor()
    cur.execute(f"SELECT id FROM queue WHERE opportunity_id = {ph} AND current_stage = 'NO_SHOW' LIMIT 1", (opp_id,))
    no_show_row = cur.fetchone()
    cur.close()
    conn.close()
    if no_show_row:
        ghl.remove_contact_tag(config["ghl_api_key"], config["ghl_location_id"], contact_id, "no show")
        update_queue(no_show_row[0], current_stage="POST_CALL", waiting_reply=0, nudge_at=None, nudge_count=0)
        # Move opportunity back to "Call Booked" stage
        stages = get_stages(config)
        call_booked_id = stages.get("Call Booked")
        if call_booked_id:
            ghl.update_opportunity_stage(config["ghl_api_key"], opp_id, call_booked_id)
        print(f"  ✓ No-show cleared for {contact_name}, restarting flow")

    # Add to queue — schedule first message 1hr after call
    added = add_to_queue(client_id, opp_id, contact_id, contact_name, phone, suburb)

    if added:
        # Schedule post-call check-in for 1 hour after call time
        try:
            tz = pytz.timezone(config.get("timezone", "Australia/Melbourne"))
            if not call_time:
                print(f"  ⚠️  WARNING: No startTime in webhook for {contact_name}. GHL may not have sent it.")
                return jsonify({"status": "no call time, cannot schedule"}), 400
            call_dt = datetime.datetime.fromisoformat(
                call_time.replace("Z", "+00:00")
            ).astimezone(tz)
            first_msg_at = call_dt + datetime.timedelta(hours=1)
            print(f"  ✓ Scheduled {contact_name}: call at {call_dt.strftime('%I:%M %p %Z')}, message at {first_msg_at.strftime('%I:%M %p %Z')}")
            from lead_queue import _get_conn, _ph
            conn, db = _get_conn()
            ph = _ph(db)
            cur = conn.cursor()
            cur.execute(
                f"SELECT id FROM queue WHERE opportunity_id = {ph} ORDER BY id DESC LIMIT 1",
                (opp_id,)
            )
            lead_id = cur.fetchone()
            cur.close()
            conn.close()
            if not lead_id:
                print(f"  ✗ [{client_id}] Could not find queued lead for opportunity {opp_id} — schedule not set")
            if lead_id:
                update_queue(lead_id[0], next_action_at=first_msg_at.astimezone(pytz.utc).isoformat())

                # If the 1-hour window has already passed (bot was offline or GHL retried the
                # webhook late), fire the POST_CALL message immediately — don't wait for the
                # scheduler's next 5-minute tick.
                now_utc = datetime.datetime.now(pytz.utc)
                if first_msg_at.astimezone(pytz.utc) <= now_utc:
                    lead_dict = {
                        "id": lead_id[0],
                        "contact_name": contact_name,
                        "contact_id": contact_id,
                        "current_stage": "POST_CALL",
                        "suburb": suburb,
                    }
                    message = get_message("POST_CALL", lead_dict, config)
                    if send(config, message, contact_id=contact_id):
                        nudge_hours = config.get("nudge_after_hours", 2)
                        update_queue(lead_id[0], waiting_reply=1, current_question="POST_CALL", nudge_count=0)
                        set_nudge(lead_id[0], nudge_hours)
                        print(f"  → [{client_id}] POST_CALL sent immediately (overdue) for {contact_name}")
                    else:
                        print(f"  ✗ [{client_id}] Failed to send POST_CALL immediately for {contact_name}")
        except Exception as e:
            print(f"  Schedule error: {e}")

    print(f"  ✓ Lead queued: {contact_name} for {config['client_name']}")
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Webhook: inbound SMS reply from Pietro
# ---------------------------------------------------------------------------

@app.route("/webhook/sms", methods=["POST"])
def webhook_sms():
    data = request.json or {}
    print(f"  RAW SMS PAYLOAD: {json.dumps(data)[:500]}")

    def safe_str(val):
        if isinstance(val, dict):
            return val.get("text") or val.get("body") or val.get("message") or str(val)
        return str(val) if val else ""

    reply = safe_str(data.get("body") or data.get("message") or "").strip()
    from_number = safe_str(data.get("phone") or data.get("fromNumber") or "")
    location_id = safe_str(data.get("locationId") or data.get("location", {}).get("id", ""))

    # Deduplicate — GHL sometimes retries the same webhook
    msg_id = safe_str(data.get("id") or data.get("messageId") or "")
    if msg_id:
        if msg_id in _processed_message_ids:
            print(f"  [DEDUP] Skipping already-processed message {msg_id}")
            return jsonify({"status": "duplicate"}), 200
        if len(_processed_message_ids) >= _MAX_DEDUP_SIZE:
            _processed_message_ids.clear()
        _processed_message_ids.add(msg_id)

    client_id, config = find_client_by_location(location_id)
    if not config:
        client_id, config = find_client_by_owner_number(from_number)
    if not config:
        return jsonify({"status": "no matching client"}), 200

    # Handle PAUSE / RESUME commands
    if reply.upper() == "PAUSE":
        pause_all(client_id)
        send(config, f"Got it {config['owner_name']}, all reminders paused. Text RESUME when you're back.")
        return jsonify({"status": "paused"}), 200

    if reply.upper() == "RESUME":
        resume_all(client_id)
        send(config, f"Welcome back {config['owner_name']}! Reminders are back on.")
        return jsonify({"status": "resumed"}), 200

    # Get the lead currently waiting on a reply
    lead = get_waiting(client_id)
    if not lead:
        print(f"  No lead waiting for reply from {config['owner_name']}")
        return jsonify({"status": "no waiting lead"}), 200

    print(f"  Reply '{reply}' for lead: {lead['contact_name']} [{lead['current_stage']}]")

    # Handle "who is this?" questions
    who_keywords = ["who is", "who's", "whos", "remind me", "who are they", "tell me about", "what do i know"]
    if any(k in reply.lower() for k in who_keywords):
        notes = ghl.get_contact_notes(config["ghl_api_key"], lead["contact_id"])
        first_name = (lead.get("contact_name") or "them").split()[0]
        if notes:
            relevant = [n for n in notes if "Lead info extracted" in n]
            note_text = relevant[-1] if relevant else notes[-1]
            send(config, f"Here's what I have on {first_name}:\n\n{note_text}")
        else:
            send(config, f"No notes on {first_name} yet.")
        return jsonify({"status": "ok"}), 200

    # Process reply
    result = handle_reply(reply, lead, config)
    next_stage = result["next_stage"]
    days = result["days"]
    opp_update = result["opp_update"]
    follow_message = result["follow_message"]

    # Update GHL opportunity using client's stage map
    stages = get_stages(config)
    stage_map = config.get("stage_map", {})
    ghl_stage_name = stage_map.get(next_stage)
    stage_id = stages.get(ghl_stage_name) if ghl_stage_name else None

    if ghl_stage_name and not stage_id:
        print(f"  ✗ [STAGE ERROR] No stage_id found for '{ghl_stage_name}' (next_stage={next_stage}) — GHL pipeline stage IDs may need updating in allworks.json")
    elif not ghl_stage_name and next_stage not in ("JOB_WON", "JOB_LOST", "WON", "LOST", "WRITEOFF"):
        print(f"  ⚠ [STAGE WARNING] No stage_map entry for next_stage={next_stage} — GHL pipeline will not be updated")

    terminal = next_stage in ("JOB_WON", "JOB_LOST", "WON", "LOST", "WRITEOFF")

    if stage_id and not terminal:
        ghl.update_opportunity_stage(config["ghl_api_key"], lead["opportunity_id"], stage_id)
    elif stage_id and next_stage == "NO_SHOW":
        # NO_SHOW should update stage but remain in queue for rescheduling
        ghl.update_opportunity_stage(config["ghl_api_key"], lead["opportunity_id"], stage_id)
    if opp_update.get("monetary_value"):
        ghl.update_opportunity_fields(
            config["ghl_api_key"],
            lead["opportunity_id"],
            monetary_value=opp_update["monetary_value"],
        )
    if opp_update.get("status") and stage_id:
        ghl.update_opportunity_stage(
            config["ghl_api_key"],
            lead["opportunity_id"],
            stage_id,
            status=opp_update["status"],
        )

    # Update queue state — clear nudge on reply
    update_queue(lead["id"], current_stage=next_stage, waiting_reply=0, nudge_at=None, nudge_count=0)
    if not terminal and next_stage != "NO_SHOW":
        set_next_action(lead["id"], days)

    # Email notifications
    lead_name = lead.get("contact_name", "Unknown")
    amount = lead.get("monetary_value")
    if next_stage == "JOB_WON":
        ghl.add_contact_tag(config["ghl_api_key"], config["ghl_location_id"], lead["contact_id"], "job won")
        notify.notify_job_won(config["client_name"], config["owner_name"], lead_name, amount)
    elif next_stage == "JOB_LOST" and lead.get("current_stage") not in ("POST_CALL", "NO_SHOW"):
        notify.notify_job_lost(config["client_name"], config["owner_name"], lead_name, amount)
    elif next_stage == "NO_SHOW":
        ghl.add_contact_tag(config["ghl_api_key"], config["ghl_location_id"], lead["contact_id"], "no show")
        # Notify owner of no-show for post-call stage
        if lead.get("current_stage") == "POST_CALL":
            notify.notify_no_show(config["client_name"], config["owner_name"], lead_name)

    # Send follow-up message if needed
    if follow_message:
        sent_ok = send(config, follow_message, contact_id=lead.get("contact_id"))
        if not sent_ok:
            print(f"  ✗ Follow-up message failed for {lead.get('contact_name')} — stage still updated, message not sent")

        # Only mark as waiting if we asked an immediate question
        if result.get("immediate_reply") and sent_ok:
            update_queue(lead["id"], waiting_reply=1, current_question=next_stage)
            set_nudge(lead["id"], config.get("nudge_after_hours", 2))

    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/debug/fire")
def debug_fire():
    """Manually trigger the scheduler tick — for testing only. Forces all due times to now."""
    from lead_queue import _get_conn, _ph
    conn, db = _get_conn()
    ph = _ph(db)
    cur = conn.cursor()
    cur.execute("UPDATE queue SET next_action_at = '2000-01-01' WHERE paused = 0 AND current_stage NOT IN ('JOB_WON','JOB_LOST','WON','LOST','WRITEOFF')")
    conn.commit()
    cur.close()
    conn.close()
    scheduler_tick()
    return jsonify({"status": "fired"})


@app.route("/debug/queue")
def debug_queue():
    """Show all leads in the queue."""
    from lead_queue import _get_conn, COLUMNS
    conn, db = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM queue")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(zip(COLUMNS, r)) for r in rows])


@app.route("/webhook/lead-message", methods=["POST"])
def webhook_lead_message():
    """Fires when the lead replies to the AI chatbot. Extracts key data and saves to GHL opportunity."""
    data = request.json or {}
    print(f"  RAW LEAD MESSAGE PAYLOAD: {json.dumps(data)[:500]}")

    contact_id = (data.get("contact_id") or data.get("contactId") or
                  data.get("contact", {}).get("id", ""))
    location_id = (data.get("locationId") or data.get("location_id") or
                   data.get("location", {}).get("id", ""))

    client_id, config = find_client_by_location(location_id)
    if not config:
        return jsonify({"status": "no matching client"}), 200

    fields = config.get("extraction_fields", [])
    if not fields:
        return jsonify({"status": "no extraction fields configured"}), 200

    # Fetch conversation from GHL
    convo_id = ghl.get_contact_conversation(config["ghl_api_key"], location_id, contact_id)
    if not convo_id:
        print(f"  — No conversation found for contact {contact_id}")
        return jsonify({"status": "no conversation found"}), 200

    messages = ghl.get_conversation_messages(config["ghl_api_key"], convo_id)
    if not messages:
        return jsonify({"status": "no messages"}), 200

    # Extract notes via Claude
    notes = brain.extract_lead_notes(messages, fields, config)
    print(f"  Extracted notes:\n{notes}")

    # Find opportunity and save note
    opp = ghl.find_opportunity(config["ghl_api_key"], location_id, contact_id)
    if opp:
        note_text = f"Lead info extracted from AI conversation:\n{notes}"
        ghl.add_opportunity_note(config["ghl_api_key"], contact_id, note_text)
        print(f"  ✓ Notes saved to opportunity {opp['id']}")
    else:
        print(f"  — No open opportunity found for contact {contact_id}")

    return jsonify({"status": "ok"}), 200


@app.route("/webhook/appointment/test", methods=["POST"])
def webhook_appointment_test():
    """Log the raw payload GHL sends — for debugging only."""
    data = request.json or {}
    print(f"  RAW APPOINTMENT PAYLOAD: {json.dumps(data)[:1000]}")
    return jsonify({"status": "logged"}), 200




@app.route("/debug/reset")
def debug_reset():
    """Clear all leads from queue — testing only."""
    from lead_queue import _get_conn
    conn, db = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM queue")
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "queue cleared"})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "evoweb-followup-bot"})


if __name__ == "__main__":
    print("Evoweb Follow-Up Bot")
    print("=" * 40)
    print("Webhooks:")
    print("  POST /webhook/appointment")
    print("  POST /webhook/sms")
    print("Scheduler running every 5 minutes")
    print("=" * 40)
    port = int(os.environ.get("PORT", 8081))
    app.run(host="0.0.0.0", port=port, debug=False)
