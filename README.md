# voice-lead-tracker

FastAPI backend for tracking voice-call sales leads. It uses SQLAlchemy and reads `DATABASE_URL` from the environment. If `DATABASE_URL` is not configured, the app falls back to local SQLite at `sqlite:///./voice_lead_tracker.db`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```
For PostgreSQL, set `DATABASE_URL` in `.env`:

```env
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/voice_lead_tracker
```

For OmniDimension call dispatch, set:

```env
OMNIDIM_API_KEY=your-api-key
OMNIDIM_AGENT_ID=your-agent-id
OMNIDIM_FROM_NUMBER_ID=your-from-number-id
```

For Unipile WhatsApp follow-ups, set:

```env
SENDER_PHONE_NUMBER=15557654321
UNIPILE_DSN=your-unipile-dsn
UNIPILE_API_KEY=your-unipile-api-key
UNIPILE_ACCOUNT_ID=your-unipile-account-id
RESUME_FILE_PATH=/path/to/resume.pdf
```

## API Examples

### Health

```bash
curl http://127.0.0.1:8000/health
```

### Create Lead

Returns `201` when a lead is created. If the `phone_number` already exists, returns the existing record with `200`.

```bash
curl -X POST http://127.0.0.1:8000/leads \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "15551234567",
    "status": "new",
    "classification": "warm",
    "budget": "$5k-$10k",
    "products": "CRM, dialer",
    "timeline": "this quarter",
    "features": "call summaries, lead scoring",
    "transcript": "Customer asked for pricing and integration options.",
    "callback_requested_at": "2026-09-02T10:30:00+05:30"
  }'
```
### Fetch One Lead

```bash
curl http://127.0.0.1:8000/leads/15551234567
```

### Dispatch Call

Looks up or creates the lead, marks it as `in_call`, and starts an outbound OmniDimension call.

```bash
curl -X POST http://127.0.0.1:8000/calls/dispatch \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "15551234567"
  }'
```

### Get Call

Returns a Call by its internal UUID:

```bash
curl http://127.0.0.1:8000/calls/00000000-0000-0000-0000-000000000000
```

### List Calls For Lead

Returns calls for a lead phone number, newest started call first:

```bash
curl http://127.0.0.1:8000/leads/15551234567/calls
```

### Classify Lead From Webhook

Looks up an existing lead by `phone_number`, updates only the fields included in the request, and appends this webhook payload as a new transcript line.

```bash
curl -X POST http://127.0.0.1:8000/webhook/classify \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "15551234567",
    "budget": "$5k-$10k",
    "products": "CRM, dialer, reporting dashboard",
    "timeline": "next two weeks",
    "features": "call summaries, lead scoring, WhatsApp follow-up",
    "classification": "hot"
  }'
```

### Send WhatsApp Follow-Up

Builds a short follow-up message from the lead's stored budget, products, timeline, and features, sends it through Unipile, optionally attaches the local resume file, and marks the lead as `follow_up_sent` with `whatsapp_sent: true` on success.

```bash
curl -X POST http://127.0.0.1:8000/webhook/whatsapp \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "15551234567"
  }'
```

### List Leads

```bash
curl "http://127.0.0.1:8000/leads?status=new&classification=warm"
```

### Update Lead

```bash
curl -X PATCH http://127.0.0.1:8000/leads/15551234567 \
  -H "Content-Type: application/json" \
  -d '{
    "status": "follow_up_pending",
    "classification": "hot",
    "timeline": "next two weeks"
  }'
```

