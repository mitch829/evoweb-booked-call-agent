# Allworks Earthworks — Follow-Up Bot Setup

## Client Config (allworks.json)

| Field | Value | Notes |
|-------|-------|-------|
| owner_name | Steven | Client owner |
| owner_mobile | +61421100840 | International format, no spaces |
| ghl_location_id | dIZXw4iqGpWZXJu5yF8p | Allworks sub-account |
| bot_phone_number | +61468167753 | Bot's sending number |
| pipeline_id | gIQoO2slQQSbpznZTdvM | "New AI Powered Sales Pipeline for Allworks Earthworks" |
| timezone | Australia/Sydney | Same as Melbourne |

## GHL Pipeline Stage IDs (hardcoded in allworks.json)

These are hardcoded to avoid GHL API inconsistencies. If stages change, update `stage_ids` in allworks.json.

| Stage Name | Stage ID |
|------------|----------|
| Call Booked | 34db48f2-4afa-4848-842d-53fc334a2db1 |
| Preparing Quote | 69cc01ff-464f-4e2d-a9a9-d07e4076bf4f |
| Quote Sent | c2ca3e98-b6bc-4d1e-986d-a87da06eae3e |
| Deposit Pending | c307b2cd-d0ad-42c0-9170-c16c16214d5e |
| Job Won | 50af9365-40cb-4f6c-a889-580f1bec036a |
| No Show | b09d0798-5310-4f7c-a959-4a2ba00a1fee |
| Job Lost | 60941d92-b3f6-45e0-b190-36478feb87ff |

## Phone Number Format

GHL requires the phone number in **international format, no spaces**: `+61421100840`

The owner_contact_id MUST match a GHL contact that has this exact phone number on file.
To verify: go to `https://software.evoweb.com.au/v2/location/dIZXw4iqGpWZXJu5yF8p/contacts/detail/e5a2uDtZ3eNAqpAxrjaD`

## Railway Deployment

- **Bot URL:** https://web-production-8d18b.up.railway.app
- **Auto-deploy:** NO — must manually redeploy in Railway after each push
- **Debug endpoints:**
  - `GET /debug/fire` — force all due messages to send now (testing only)
  - `GET /debug/queue` — view all leads in queue
  - `GET /debug/reset` — clear all leads (does NOT send any messages)

## Complete Sales Flow

### POST_CALL (GHL: Call Booked)
Fires 1 hour after appointment. Steven replies:
- **1 = Preparing quote** → GHL: Preparing Quote → ask for quote amount in 2 days
- **2 = Need site visit** → GHL: Preparing Quote → ask about site visit in 3 days
- **3 = No show** → GHL: No Show → email to Mitch + no-show tag + bot goes silent
- **4 = Not interested** → GHL: Job Lost → unqualified tag

### SITE_VISIT (GHL: Preparing Quote)
3 days after "need site visit" reply:
- **DONE** → ask for quote amount immediately
- **NOT YET** → retry in 2 days
- **LOST** → Job Lost

### ASK_QUOTE_AMOUNT (GHL: Preparing Quote)
- **Amount given (e.g. $25,000)** → log amount → ask if quote sent in 2 days
- **No amount** → retry tomorrow

### QUOTE_CONFIRM (GHL: Preparing Quote)
- **YES** → GHL: Quote Sent → check back in 3 days
- **NOT YET** → retry tomorrow
- **LOST** → Job Lost

### QUOTE_SENT (GHL: Quote Sent)
- **1 = He's keen** → GHL: Deposit Pending
- **2 = Still chasing** → FOLLOW_1 in 3 days
- **3 = Lost him** → Job Lost

### DEPOSIT_PENDING (GHL: Deposit Pending)
- **YES** → GHL: Job Won + email to Mitch + job won tag 🎉
- **NOT YET** → retry in 3 days
- **LOST** → Job Lost

### FOLLOW_1 → FOLLOW_7 (GHL: Quote Sent)
Follow-up intervals: 3, 5, 7, 10, 12, 14, 21 days
- **1 = He's in** → Deposit Pending
- **2 = Still chasing** → next FOLLOW stage
- **3 = Write him off** → Job Lost

### NO_SHOW Rebook
When new appointment booked after no-show:
- GHL resets to Call Booked
- No-show tag removed
- Flow restarts from POST_CALL

## Tags Applied
| Trigger | Tag |
|---------|-----|
| POST_CALL → not interested | unqualified |
| Any stage → no show | no show |
| Any stage → job won | job won |
| End of follow-up sequence → lost | job lost |

## Email Notifications (to mitch@evoweb.com.au)
| Trigger | Email Subject |
|---------|---------------|
| No show (POST_CALL only) | No Show Alert — Allworks Earthworks |
| Job Won | Job Won ($amount) — Allworks Earthworks |
| Job Lost (after follow-up sequence) | Job Lost — Allworks Earthworks |

## Troubleshooting

**SMS not sending:**
- Check owner_mobile is in format `+61XXXXXXXXX` (no spaces)
- Check Railway is running (visit /health endpoint)

**Stages not updating:**
- Stage IDs are hardcoded in `stage_ids` in allworks.json
- If GHL pipeline stages change, get new IDs and update allworks.json

**Scheduler not firing:**
- Check Railway logs for `[SCHEDULER] Tick started`
- Check business hours: only sends 8am–8pm Sydney time
- Use /debug/fire to manually trigger

**Messages firing at wrong time:**
- 1 hour after appointment is calculated from GHL's `startTime` field
- Timezone is Australia/Sydney (same as Melbourne)
