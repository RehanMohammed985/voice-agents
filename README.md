<div align="center">

# voice-agents

**Give it plain instructions. It calls the companies, follows the mission, takes notes, and books meetings.**

As I build my startup, calls can get annoying when you have so much on your plate. hence why this exists!!

An open-source AI cold-calling console. Write a mission in plain English, point it at a
call list, hit launch — the agent dials each number, discloses it's an AI, runs your
instructions, captures structured notes, and schedules meetings. Built to run on free
voice-platform credits (Vapi + Deepgram), with a web UI and a clean HTTP API so you can
also drive it from your own app server.

`FastAPI` · `Vapi` / `Bland` engines · `Deepgram` STT · MIT

</div>

---

## What it does

1. **You write a mission** — e.g. *"Call each company, reach whoever runs operations, ask
   how they handle X, and book a 20-minute call if they're interested."* Five customer-discovery
   templates (problem discovery, willingness to pay, why they switched, book the meeting) are one
   click away, and you set how long each call may run.
2. **You give it a call list** — paste numbers one per line (bare, or `Company, number`),
   or upload a CSV. Duplicates are dropped.
3. **It runs the campaign** — dials each number through a real voice platform, has the
   conversation, and after each call extracts structured fields (who you reached, their
   answers, whether a meeting was booked, their email, sentiment, a summary).
4. **You watch it come in** — a live log and a notes/meetings table update as calls complete;
   results also persist to `data/results_*.csv`.

**Dry-run by default.** Nothing dials until you flip the LIVE switch *and* the selected
engine's keys are present. Guardrails live in the engine, not the UI, so they can't be
skipped: AI self-disclosure on every call, business-hours-only by the number's timezone,
a recording-consent line in two-party-consent states, and do-not-call skipping.
**Read [`COMPLIANCE.md`](COMPLIANCE.md) before going live.**

## Screenshots

The console is one page: **mission** (plain-English instructions → compiled prompt),
**call list** (add/upload targets), **launch** (engine + limit + LIVE toggle + a live log),
and **notes & meetings** (structured results as they land).

## Quick start

```bash
git clone https://github.com/RehanMohammed985/voice-agents
cd voice-agents
pip install -r requirements.txt
cp .env.example .env            # add engine keys when you're ready to go live
uvicorn server.app:app --reload
# open http://localhost:8000
```

The UI works immediately in **dry-run** with no keys — it validates your mission, list,
timezones, and consent flags, and shows exactly who *would* be called. Add keys to go live.

## Deploy it

`vercel` — see [`DEPLOY.md`](DEPLOY.md). On a serverless host the app switches itself into
**webhook mode**: calls are dispatched in one non-blocking pass and Vapi POSTs each
end-of-call report to `/api/webhook/vapi`, instead of a thread polling until every call
ends. State moves from local JSON to Upstash Redis. Both switches are a single env var
each (`PUBLIC_BASE_URL`, `UPSTASH_REDIS_REST_URL`), and neither changes local development —
with them unset you get files and polling exactly as before. `GET /api/health` tells you
which mode a running instance is in.

## Run it on free credits

You can run hundreds of real calls without paying cash. See [`CREDITS.md`](CREDITS.md) for the
full allocation; the short version:

| Credit | Role |
|---|---|
| **Vapi** ($100 + $250 demo) | primary engine — includes a **free phone number** |
| **Deepgram** ($15k) | attach your key in Vapi → transcription bills here, not Vapi |
| **Bolna** (2,000 min) | free-minutes backup engine |

Set `VAPI_API_KEY` + `VAPI_PHONE_NUMBER_ID` in `.env`, pick **Vapi** in the UI, flip LIVE.

## Use it from your own app server (the API)

The web UI is just a client of a small HTTP API — wire your product to the same endpoints:

| Method | Route | Does |
|---|---|---|
| `GET`  | `/api/config` | engine readiness + current mission + compiled prompt |
| `POST` | `/api/mission` | set instructions / founder / booking link (returns compiled prompt) |
| `GET`  | `/api/targets` | list the call list |
| `POST` | `/api/targets` | add one `{company, phone, notes}` |
| `POST` | `/api/targets/bulk` | paste a list — `{text, replace}`, any format, deduped |
| `POST` | `/api/targets/upload` | replace the list from a CSV upload |
| `DELETE` | `/api/targets` | clear the list |
| `POST` | `/api/campaign` | launch `{engine, live, limit, ignore_hours}` |
| `GET`  | `/api/status` | live campaign state + streamed log + results |
| `POST` | `/api/webhook/vapi` | Vapi's end-of-call report lands here (webhook mode) |
| `GET`  | `/api/health` | run mode + storage backend |

So your "give it simple instructions, it calls and books" flow is one `POST /api/mission`
followed by `POST /api/campaign`, then poll `GET /api/status`. Interactive docs at `/api/docs`.

## Engines

Three interchangeable runners share one script, one target list, and identical guardrails:

- **`caller/vapi.py`** — Vapi (recommended; free-credit path, transient inline assistant).
- **`caller/campaign.py`** — Bland AI.
- **`caller/eleven.py`** — ElevenLabs Conversational AI (best voices; see `AGENT_SETUP.md`).

The web server (`server/app.py`) drives Vapi and Bland directly; ElevenLabs runs from the CLI.
Swapping engines is three functions (`dispatch` / `fetch` / `extract`) — everything else is
vendor-neutral.

## The mission → prompt compiler

Your instructions are wrapped with non-negotiable guardrails and a meetings block, then
handed to the voice model as its system prompt. You can see the exact compiled prompt in the
UI ("view compiled prompt") or via `GET /api/config`. Edit the guardrail wrapper in
`server/app.py` (`GUARDRAILS`) and the default script in `caller/script.py`.

## Project layout

```
voice-agents/
├─ server/
│  ├─ app.py                 # FastAPI: mission compiler, campaign runner, webhook, status
│  ├─ store.py               # state: local JSON files, or Upstash Redis when deployed
│  └─ static/index.html      # the console (single file, no build step)
├─ api/index.py              # Vercel entry point
├─ vercel.json
├─ caller/
│  ├─ script.py              # default mission, voicemail, extraction schema
│  ├─ vapi.py                # Vapi engine (recommended)
│  ├─ campaign.py            # Bland engine
│  └─ eleven.py              # ElevenLabs engine
├─ data/
│  └─ targets.csv            # your call list (seeded with 21 real solar-O&M numbers)
├─ COMPLIANCE.md   ·  CREDITS.md  ·  AGENT_SETUP.md
├─ .env.example    ·  requirements.txt  ·  LICENSE (MIT)
```

## Responsible use

This tool is for **B2B research and appointment-setting to business numbers**, with an agent
that **identifies itself as an AI**. Don't call cell/residential lines with it, don't remove
the disclosure, honor every opt-out, and check your local AI-calling and recording laws.
See [`COMPLIANCE.md`](COMPLIANCE.md). You are responsible for how you use it.

## License

MIT © 2026 Rehan Mohammed
