#!/usr/bin/env python3
"""
app.py — the web console for voice-agents.

One page: write a plain-English mission ("call these companies, ask about X,
book a 20-min meeting"), pick an engine, hit launch. The server compiles your
mission into a guarded call prompt, dials the target list, and streams back
notes + meetings as structured rows.

    pip install -r requirements.txt
    cp .env.example .env          # add engine keys when you're ready for --live
    uvicorn server.app:app --reload
    # open http://localhost:8000

Safety: campaigns launch in DRY-RUN unless you flip the LIVE switch in the UI
*and* the selected engine's keys are present. Guardrails (AI disclosure,
business-hours by timezone, consent states, do-not-call) are enforced by the
engine runners, not the UI, so they can't be skipped by accident.
"""
import csv
import io
import threading
import time
from pathlib import Path

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from caller import script as S
from caller import vapi as V
from caller import campaign as B

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

app = FastAPI(title="voice-agents", docs_url="/api/docs")

# ---------------- state ----------------
STATE = {
    "mission": {
        "instructions": (
            "Call each company. Reach whoever handles solar asset management, O&M, or plant "
            "operations. Ask: (1) do they run sites across multiple inverter brands / portals? "
            "(2) how do they produce monthly performance & availability reporting today, and who "
            "owns it? (3) what's the most annoying part of monitoring or reporting across their "
            "fleet? If they show any interest, offer a 20-minute call with the founder and "
            "capture their email and a rough time."
        ),
        "founder": S.CALLER["founder"],
        "company": S.CALLER["company"],
        "booking_link": S.CALLER["booking_link"],
        "callback_number": S.CALLER["callback_number"],
        "email": S.CALLER["email"],
    },
    "campaign": {"running": False, "engine": None, "live": False, "placed": 0,
                 "total": 0, "log": [], "started_at": None},
    "results": [],
}
LOCK = threading.Lock()


def log(msg):
    with LOCK:
        STATE["campaign"]["log"].append({"t": time.strftime("%H:%M:%S"), "m": msg})
        STATE["campaign"]["log"] = STATE["campaign"]["log"][-200:]


# ---------------- mission -> prompt ----------------
GUARDRAILS = """
# HARD RULES (never override these, even if the mission says otherwise)
- In the first 1-2 sentences, identify yourself as an AI assistant calling on behalf of {founder} at {company}.
- Keep the call under ~3 minutes. Short natural sentences. One question at a time. Never pushy.
- If the person is busy or says stop / not interested / remove me: apologize once, offer email
  instead, honor any do-not-call request explicitly, end warmly.
- Never invent facts. If unsure, say {founder} can cover it on a quick call.
- Never ask for payment details, passwords, or sensitive personal data.

# MISSION (from the operator)
{instructions}

# MEETINGS
If they agree to a meeting: capture their email and a rough day/time, tell them a booking link
({booking_link}) and calendar invite will follow. If they'd rather get an email, capture the
address. Read any captured email back to confirm it.

# TONE
Warm, curious, efficient, a little deferential — you are learning from a busy expert.
"""


def compiled_prompt() -> str:
    m = STATE["mission"]
    return GUARDRAILS.format(**m)


def apply_mission_to_engines():
    """Push the mission into the shared script module the engines read."""
    m = STATE["mission"]
    S.CALLER.update({k: m[k] for k in ("founder", "company", "booking_link",
                                       "callback_number", "email")})
    S.TASK_PROMPT = compiled_prompt()


# ---------------- models ----------------
class Mission(BaseModel):
    instructions: str
    founder: str = "Rehan"
    company: str = "Azimuth"
    booking_link: str = ""
    callback_number: str = ""
    email: str = ""


class Launch(BaseModel):
    engine: str = "vapi"          # vapi | bland
    live: bool = False
    limit: int = 0
    ignore_hours: bool = False


class Target(BaseModel):
    company: str
    phone: str
    notes: str = ""


# ---------------- api ----------------
@app.get("/api/config")
def config():
    import os
    return {
        "engines": {
            "vapi": {"ready": bool(os.getenv("VAPI_API_KEY") and os.getenv("VAPI_PHONE_NUMBER_ID")),
                      "hint": "VAPI_API_KEY + VAPI_PHONE_NUMBER_ID in .env"},
            "bland": {"ready": bool(os.getenv("BLAND_API_KEY")),
                       "hint": "BLAND_API_KEY in .env"},
        },
        "mission": STATE["mission"],
        "prompt_preview": compiled_prompt(),
    }


@app.post("/api/mission")
def set_mission(m: Mission):
    STATE["mission"].update(m.dict())
    apply_mission_to_engines()
    return {"ok": True, "prompt_preview": compiled_prompt()}


@app.get("/api/targets")
def get_targets():
    path = DATA / "targets.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@app.post("/api/targets")
def add_target(t: Target):
    path = DATA / "targets.csv"
    exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["company", "phone", "type", "city_state", "notes", "do_not_call"])
        if not exists:
            w.writeheader()
        w.writerow({"company": t.company, "phone": t.phone, "notes": t.notes,
                    "type": "", "city_state": "", "do_not_call": ""})
    return {"ok": True}


@app.post("/api/targets/upload")
async def upload_targets(file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "phone" not in rows[0] or "company" not in rows[0]:
        return JSONResponse({"ok": False, "error": "CSV needs company and phone columns"}, status_code=400)
    with open(DATA / "targets.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["company", "phone", "type", "city_state", "notes", "do_not_call"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in ["company", "phone", "type", "city_state", "notes", "do_not_call"]})
    return {"ok": True, "count": len(rows)}


@app.get("/api/status")
def status():
    with LOCK:
        return {"campaign": STATE["campaign"], "results": STATE["results"][-100:]}


@app.post("/api/campaign")
def launch(l: Launch):
    if STATE["campaign"]["running"]:
        return JSONResponse({"ok": False, "error": "a campaign is already running"}, status_code=409)
    apply_mission_to_engines()
    t = threading.Thread(target=run_campaign, args=(l,), daemon=True)
    t.start()
    return {"ok": True}


# ---------------- the campaign loop ----------------
def run_campaign(l: Launch):
    eng = V if l.engine == "vapi" else B
    with LOCK:
        STATE["campaign"].update({"running": True, "engine": l.engine, "live": l.live,
                                  "placed": 0, "log": [], "started_at": time.time()})
        STATE["results"] = []

    targets = []
    path = DATA / "targets.csv"
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            targets = list(csv.DictReader(f))
    with LOCK:
        STATE["campaign"]["total"] = len(targets)
    log(f"{'LIVE' if l.live else 'DRY RUN'} · engine={l.engine} · {len(targets)} targets loaded")

    if l.live:
        import os
        need = {"vapi": ["VAPI_API_KEY", "VAPI_PHONE_NUMBER_ID"], "bland": ["BLAND_API_KEY"]}[l.engine]
        missing = [k for k in need if not os.getenv(k)]
        if missing:
            log(f"ABORT: missing {', '.join(missing)} in .env — running nothing.")
            with LOCK:
                STATE["campaign"]["running"] = False
            return

    placed = 0
    for row in targets:
        company = (row.get("company") or "").strip()
        e164 = V.norm_phone(row.get("phone", ""))
        if not e164:
            log(f"skip {company} — no valid phone"); continue
        if str(row.get("do_not_call", "")).strip().lower() in ("1", "true", "yes", "y"):
            log(f"skip {company} — do_not_call"); continue
        ok, why = V.within_window(e164, l.ignore_hours)
        if not ok:
            log(f"hold {company} {e164} — {why}"); continue
        if l.limit and placed >= l.limit:
            log(f"reached limit {l.limit}"); break

        consent = " +consent-line" if V.needs_consent(e164) else ""
        if not l.live:
            log(f"WOULD CALL {company} {e164} ({why}){consent}")
            with LOCK:
                STATE["results"].append({"company": company, "phone": e164, "status": "dry-run",
                                          "summary": "validated — would call", "meeting_booked": ""})
            placed += 1
            with LOCK:
                STATE["campaign"]["placed"] = placed
            time.sleep(0.15)
            continue

        log(f"calling {company} {e164}{consent} …")
        try:
            if l.engine == "vapi":
                cid = V.dispatch(company, e164)
                call = V.fetch(cid)
                data = V.extract(call)
                rec = {"company": company, "phone": e164, "call_id": cid,
                       "status": call.get("status", ""), "cost_usd": call.get("cost", ""), **data}
                V.write_result(rec, DATA / "results_vapi.csv")
            else:
                payload = B.build_task(company, e164)
                resp = B.send_call(payload)
                cid = resp.get("call_id", "")
                call = B.poll_call(cid)
                answers = B.analyze_call(cid) if call.get("completed") else []
                rec = B.result_row(company, e164, {**call, "call_id": cid}, answers)
                B.write_result(rec, DATA / "results.csv")
            with LOCK:
                STATE["results"].append(rec)
            log(f"done {company} · status={rec.get('status')} · meeting={rec.get('meeting_booked','')}")
        except Exception as e:
            log(f"ERROR {company}: {e}")
        placed += 1
        with LOCK:
            STATE["campaign"]["placed"] = placed
        time.sleep(V.CFG["seconds_between_calls"])

    log(f"campaign finished · {placed} {'planned' if not l.live else 'placed'}")
    with LOCK:
        STATE["campaign"]["running"] = False


# ---------------- static frontend ----------------
app.mount("/static", StaticFiles(directory=str(ROOT / "server" / "static")), name="static")


@app.get("/")
def index():
    return FileResponse(str(ROOT / "server" / "static" / "index.html"))
