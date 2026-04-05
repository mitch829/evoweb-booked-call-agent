"""
Claude brain — interprets Pietro's replies and drafts messages.
"""

import re
import os
import anthropic
from dotenv import load_dotenv

load_dotenv()
_api_key = os.environ.get("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
if not _api_key:
    raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set")
_client = anthropic.Anthropic(api_key=_api_key)

VALID_STAGES = [
    "POST_CALL", "SITE_VISIT", "ASK_QUOTE_AMOUNT", "QUOTE_CONFIRM",
    "QUOTE_SENT", "DEPOSIT_PENDING",
    "FOLLOW_1", "FOLLOW_2", "FOLLOW_3", "FOLLOW_4", "FOLLOW_5", "FOLLOW_6", "FOLLOW_7",
    "NO_SHOW", "JOB_WON", "JOB_LOST",
    # legacy
    "QUOTING", "FOLLOWING_UP", "LAST_CHANCE", "WON", "LOST", "WRITEOFF",
    "NOT_INTERESTED", "RESCHEDULE", "TRY", "DONE", "NOT_YET",
    "KEEN", "CHASING", "STILL", "SENT", "PAID", "YES",
]


def _load_notes(config):
    """Load the client knowledge file if it exists."""
    import pathlib
    client_id = config.get("_client_id", "")
    notes_path = pathlib.Path(__file__).parent / "clients" / f"{client_id}_notes.md"
    if notes_path.exists():
        return notes_path.read_text()
    return ""


def interpret_reply(reply_text, current_stage, lead, config):
    owner = config["owner_name"]
    name = lead.get("contact_name", "the lead")
    notes = _load_notes(config)
    notes_section = f"\n\nBusiness context:\n{notes}" if notes else ""

    prompt = f"""You are interpreting an SMS reply from {owner}, who runs {config['client_name']}.{notes_section}

Lead: {name} | Current stage: {current_stage}
{owner}'s reply: "{reply_text}"

Determine:
1. next_stage: the most appropriate next stage
2. days: days until next follow-up message
3. note: one sentence summary for CRM notes

Stage mapping guide:
POST_CALL replies:
- "1" or preparing/positive → QUOTING
- "2" or site visit → SITE_VISIT
- "3" or no show → NO_SHOW
- "4" or not interested → JOB_LOST

SITE_VISIT replies:
- "done" or yes/in quote → DONE
- "not yet" → NOT_YET
- lost → JOB_LOST

ASK_QUOTE_AMOUNT replies:
- any dollar amount → QUOTE_CONFIRM (handle in code)

QUOTE_CONFIRM replies:
- "yes" or sent → SENT
- "not yet" → NOT_YET
- lost → JOB_LOST

QUOTE_SENT replies:
- "1" or keen → KEEN
- "2" or still chasing → CHASING
- "3" or lost → JOB_LOST

DEPOSIT_PENDING replies:
- "yes" or paid → PAID
- "not yet" → NOT_YET
- lost → JOB_LOST

FOLLOW_X replies:
- "1" or won/keen → JOB_WON
- "2" or still/try/chasing → STILL
- "3" or lost/done/writeoff → JOB_LOST
- "try" in FOLLOW_6 → TRY

NO_SHOW replies:
- "yes" or reschedule → YES
- "no" → JOB_LOST

Reply in exactly this format:
next_stage: <VALUE>
days: <NUMBER>
note: <NOTE>"""

    try:
        response = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
    except Exception as e:
        print(f"  [BRAIN ERROR] Claude API call failed: {e} — keeping current stage {current_stage}")
        return {"next_stage": current_stage, "days": 3, "note": ""}

    result = {"next_stage": None, "days": 3, "note": ""}
    for line in text.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key == "next_stage":
                result["next_stage"] = val
            elif key == "days":
                try:
                    result["days"] = int(val)
                except ValueError:
                    pass
            elif key == "note":
                result["note"] = val

    if not result["next_stage"]:
        print(f"  [BRAIN WARNING] Claude returned no next_stage. Raw: {text[:200]} — keeping current stage {current_stage}")
        result["next_stage"] = current_stage

    return result


def extract_amount(text):
    """Pull a dollar amount from Pietro's reply."""
    text = text.replace(",", "").replace("$", "").strip()
    match = re.search(r'\b(\d+(?:\.\d{1,2})?)\b', text)
    if match:
        return float(match.group(1))
    return None


def extract_lead_notes(messages, fields, config):
    """
    Extract key lead data from a conversation transcript.
    messages: list of {direction, body} dicts
    fields: list of field names to extract e.g. ["wall length", "postcode", "has plans"]
    Returns a formatted string of extracted notes.
    """
    transcript = "\n".join(
        f"{'Lead' if m.get('direction') == 'inbound' else 'Bot'}: {m.get('body', '')}"
        for m in messages if m.get("body")
    )
    fields_list = "\n".join(f"- {f}" for f in fields)
    notes = _load_notes(config)
    notes_section = f"\n\nBusiness context:\n{notes}" if notes else ""

    prompt = f"""You are reviewing a conversation between an AI chatbot and a lead for {config['client_name']}.{notes_section}

Extract the following information from the conversation. If a field is not mentioned, write "Not mentioned".

Fields to extract:
{fields_list}

Conversation:
{transcript}

Reply in exactly this format (one per line):
field: value"""

    response = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def draft_followup(lead, config):
    """Draft a follow-up text the owner can copy and send to the lead."""
    owner = config["owner_name"]
    name = lead.get("contact_name", "").split()[0] or "mate"
    service = config.get("niche", "the work")
    notes = _load_notes(config)
    notes_section = f"\n\nBusiness context:\n{notes}" if notes else ""

    response = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": (
            f"Write a short casual follow-up SMS from {owner} to {name}. "
            f"{owner} sent a quote for {service} a few days ago and hasn't heard back. "
            f"1-2 sentences, no pressure, friendly. Sign off with just '{owner}'."
            f"{notes_section}\n\n"
            f"Reply with message text only."
        )}],
    )
    return response.content[0].text.strip()


def extract_booked_call_data(messages, fields, config, contact_name):
    """
    Extract lead data from a booked call conversation.
    Returns both formatted text and structured dict for custom fields.
    """
    extracted = extract_lead_notes(messages, fields, config)

    # Format nicely for copy/paste
    lines = [f"\n=== {config.get('client_name', 'Client')} Lead Data ===\n"]
    lines.append(f"Lead: {contact_name}")
    lines.append(extracted)
    lines.append("\n=== Ready to copy/paste to WhatsApp ===\n")

    formatted_text = "\n".join(lines)

    # Parse extracted data into dict for custom fields
    # Format from extract_lead_notes is "field: value" per line
    custom_fields_dict = {}
    for line in extracted.split('\n'):
        if ':' in line:
            key, val = line.split(':', 1)
            field_name = key.strip().lower().replace(" ", "_").replace("(", "").replace(")", "")
            custom_fields_dict[field_name] = val.strip()

    return {
        "formatted_text": formatted_text,
        "custom_fields": custom_fields_dict,
    }
