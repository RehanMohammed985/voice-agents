#!/usr/bin/env python3
"""
app.py — the web console for voice-agents.

One page: write a plain-English mission ("call these companies, ask about X,
book a 20-min meeting"), pick an engine, hit launch. The server compiles your
mission into a guarded call prompt, dials the target list, and collects notes +
meetings as structured rows.

    pip install -r requirements.txt
    cp .env.example .env          # add engine keys when you're ready to go live
    uvicorn server.app:app --reload
    # open http://localhost:8000

TWO RUN MODES — the app picks one for you:

  polling mode   (default, local)   A background thread dials each number and
                                    waits for the call to finish. Nothing to
                                    configure; works on localhost.

  webhook mode   (set PUBLIC_BASE_URL, e.g. on Vercel)
                                    Calls are dispatched and the request
                                    returns immediately; Vapi POSTs each
                                    end-of-call report to /api/webhook/vapi.
                                    Required on serverless, where background
                                    threads are killed after the response.

State lives in server/store.py — JSON files locally, Upstash Redis when its
env vars are present. See DEPLOY.md.

Safety: campaigns launch in DRY-RUN unless you flip the LIVE switch in the UI
*and* the selected engine's keys are present. Guardrails (AI disclosure,
business-hours by timezone, consent states, do-not-call) are enforced by the
engine runners, not the UI, so they can't be skipped by accident.
"""
import csv
import io
import os
import re
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from caller import campaign as B
from caller import script as S
from caller import vapi as V
from server import store

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

app = FastAPI(title="voice-agents", docs_url="/api/docs")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
LOG_CAP = 200


def webhook_mode() -> bool:
    """True when Vapi can reach us — then we never block waiting on a call."""
    return bool(PUBLIC_BASE_URL)


def webhook_url() -> str:
    return f"{PUBLIC_BASE_URL}/api/webhook/vapi" if PUBLIC_BASE_URL else ""


# ---------------- defaults ----------------
DEFAULT_MISSION = {
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
    "duration_min": 3,
}
IDLE_CAMPAIGN = {"running": False, "engine": None, "live": False, "placed": 0,
                 "total": 0, "started_at": None}


def mission() -> dict:
    m = dict(DEFAULT_MISSION)
    m.update(store.get("mission", {}) or {})
    return m


def log(msg):
    store.append("log", {"t": time.strftime("%H:%M:%S"), "m": msg}, cap=LOG_CAP)


# ---------------- mission -> prompt ----------------
GUARDRAILS = """
# HARD RULES (never override these, even if the mission says otherwise)
- In the first 1-2 sentences, identify yourself as an AI assistant calling on behalf of {founder} at {company}.
- Keep the call under ~{duration_min} minutes. Short natural sentences. One question at a time. Never pushy.
- If the person is busy or says stop / not interested / remove me: apologize once, offer email
  instead, honor any do-not-call request explicitly, end warmly.
- Never invent facts. If unsure, say {founder} can cover it on a quick call.
- Never ask for payment details, passwords, or sensitive personal data.

# MISSION (from the operator)
{instructions}

# MEETINGS
If they agree to a meeting: capture their email and a rough day/time, tell them a booking link
({booking_link}) and calendar invite will follow. If they'd rather get an email, capture the
address. Read any captured email back to confirm.

# TONE
Warm, curious, efficient, a little deferential — you are learning from a busy expert.
"""


def compiled_prompt(m=None) -> str:
    return GUARDRAILS.format(**(m or mission()))


def apply_mission_to_engines(m=None):
    """Push the mission into the shared script module the engines read."""
    m = m or mission()
    S.CALLER.update({k: m[k] for k in ("founder", "company", "booking_link",
                                       "callback_number", "email")})
    S.TASK_PROMPT = compiled_prompt(m)
    mins = max(1, min(15, int(m.get("duration_min") or 3)))
    V.CFG["max_duration_s"] = mins * 60
    B.CFG["max_duration_min"] = mins


# ---------------- models ----------------
class Mission(BaseModel):
    instructions: str
    founder: str = "Rehan"
    company: str = "Azimuth"
    booking_link: str = ""
    callback_number: str = ""
    email: str = ""
    duration_min: int = 3


class Launch(BaseModel):
    engine: str = "vapi"          # vapi | bland
    live: bool = False
    limit: int = 0
    ignore_hours: bool = False


class Target(BaseModel):
    company: str
    phone: str
    notes: str = ""


class Bulk(BaseModel):
    text: str
    replace: bool = False


# ---------------- targets ----------------
TFIELDS = ["company", "phone", "type", "city_state", "notes", "do_not_call"]


def _seed_targets():
    """First boot: use the CSV shipped in the repo, then keep state in the store."""
    path = DATA / "targets.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: r.get(k, "") for k in TFIELDS} for r in csv.DictReader(f)]


def targets() -> list:
    t = store.get("targets")
    if t is None:
        t = _seed_targets()
        store.set("targets", t)
    return t


def set_targets(rows):
    clean = [{k: r.get(k, "") for k in TFIELDS} for r in rows]
    store.set("targets", clean)
    try:                                   # keep the CSV in sync when disk is writable
        with open(DATA / "targets.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TFIELDS)
            w.writeheader()
            w.writerows(clean)
    except OSError:
        pass
    return clean


# ---------------- api ----------------
@app.get("/api/config")
def config():
    m = mission()
    return {
        "engines": {
            "vapi": {"ready": bool(os.getenv("VAPI_API_KEY") and os.getenv("VAPI_PHONE_NUMBER_ID")),
                     "hint": "VAPI_API_KEY + VAPI_PHONE_NUMBER_ID"},
            "bland": {"ready": bool(os.getenv("BLAND_API_KEY")),
                      "hint": "BLAND_API_KEY"},
        },
        "mission": m,
        "prompt_preview": compiled_prompt(m),
        "runtime": {"mode": "webhook" if webhook_mode() else "polling",
                    "store": store.backend(),
                    "webhook_url": webhook_url()},
    }


@app.post("/api/mission")
def set_mission(m: Mission):
    merged = store.merge("mission", m.dict())
    full = dict(DEFAULT_MISSION); full.update(merged)
    apply_mission_to_engines(full)
    return {"ok": True, "prompt_preview": compiled_prompt(full)}


@app.get("/api/targets")
def get_targets():
    return targets()


@app.post("/api/targets")
def add_target(t: Target):
    rows = targets() + [{"company": t.company, "phone": V.norm_phone(t.phone),
                         "notes": t.notes, "type": "", "city_state": "", "do_not_call": ""}]
    set_targets(rows)
    return {"ok": True}


@app.post("/api/targets/bulk")
def bulk_targets(b: Bulk):
    """Paste a list. One per line: '+15551234567' or 'Acme Solar, +1 555 123 4567'."""
    rows = []
    for line in b.text.splitlines():
        line = line.strip().strip(",")
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"[,\t;|]", line) if p.strip()]
        phone = next((p for p in parts if len(re.sub(r"\D", "", p)) >= 10), "")
        if not phone:
            continue
        company = next((p for p in parts if p != phone), "") or "—"
        rows.append({"company": company, "phone": V.norm_phone(phone),
                     "type": "", "city_state": "", "notes": "", "do_not_call": ""})
    if not rows:
        return JSONResponse({"ok": False, "error": "no valid phone numbers found"}, status_code=400)

    base = [] if b.replace else targets()
    seen, merged = set(), []
    for r in base + rows:
        ph = (r.get("phone") or "").strip()
        if not ph or ph in seen:
            continue
        seen.add(ph)
        merged.append(r)
    set_targets(merged)
    return {"ok": True, "added": len(rows), "total": len(merged)}


@app.post("/api/targets/upload")
async def upload_targets(file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "phone" not in rows[0] or "company" not in rows[0]:
        return JSONResponse({"ok": False, "error": "CSV needs company and phone columns"}, status_code=400)
    for r in rows:
        r["phone"] = V.norm_phone(r.get("phone", ""))
    set_targets(rows)
    return {"ok": True, "count": len(rows)}


@app.delete("/api/targets")
def clear_targets():
    set_targets([])
    return {"ok": True}


@app.get("/api/status")
def status():
    c = dict(IDLE_CAMPAIGN)
    c.update(store.get("campaign", {}) or {})
    results = store.get("results", []) or []
    # in webhook mode nothing tells us "done" — the call count does
    if c.get("running") and c.get("dispatched") and len(results) >= c["dispatched"]:
        c["running"] = False
        store.merge("campaign", {"running": False})
    c["log"] = store.get("log", []) or []
    return {"campaign": c, "results": results[-100:]}


@app.post("/api/campaign")
def launch(l: Launch):
    cur = store.get("campaign", {}) or {}
    if cur.get("running"):
        return JSONResponse({"ok": False, "error": "a campaign is already running"}, status_code=409)
    apply_mission_to_engines()

    if webhook_mode() or not l.live:
        return _run(l, blocking=True)          # fast: dispatch (or validate) inline
    threading.Thread(target=_run, args=(l,), daemon=True).start()
    return {"ok": True}


# ---------------- the campaign ----------------
def _eligible(l: Launch):
    """Yield (company, e164, why, consent) for every target we may legally call now."""
    for row in targets():
        company = (row.get("company") or "").strip()
        e164 = V.norm_phone(row.get("phone", ""))
        if not e164:
            log(f"skip {company} — no valid phone"); continue
        if str(row.get("do_not_call", "")).strip().lower() in ("1", "true", "yes", "y"):
            log(f"skip {company} — do_not_call"); continue
        ok, why = V.within_window(e164, l.ignore_hours)
        if not ok:
            log(f"hold {company} {e164} — {why}"); continue
        yield company, e164, why, V.needs_consent(e164)


def _run(l: Launch, blocking=False):
    all_targets = targets()
    store.set("log", [])
    store.set("results", [])
    store.set("campaign", {"running": True, "engine": l.engine, "live": l.live,
                           "placed": 0, "total": len(all_targets), "dispatched": 0,
                           "started_at": time.time()})
    mode = "webhook" if webhook_mode() else "polling"
    log(f"{'LIVE' if l.live else 'DRY RUN'} · engine={l.engine} · {len(all_targets)} targets · {mode} mode")

    if l.live:
        need = {"vapi": ["VAPI_API_KEY", "VAPI_PHONE_NUMBER_ID"], "bland": ["BLAND_API_KEY"]}[l.engine]
        missing = [k for k in need if not os.getenv(k)]
        if missing:
            log(f"ABORT: missing {', '.join(missing)} — running nothing.")
            store.merge("campaign", {"running": False})
            return {"ok": False, "error": f"missing {', '.join(missing)}"}
        if webhook_mode() and l.engine == "bland":
            log("note: webhook mode currently returns results for Vapi only.")

    placed = 0
    for company, e164, why, consent in _eligible(l):
        if l.limit and placed >= l.limit:
            log(f"reached limit {l.limit}"); break
        tag = " +consent-line" if consent else ""

        if not l.live:
            log(f"WOULD CALL {company} {e164} ({why}){tag}")
            store.append("results", {"company": company, "phone": e164, "status": "dry-run",
                                     "summary": "validated — would call", "meeting_booked": ""})
            placed += 1
            store.merge("campaign", {"placed": placed, "dispatched": placed})
            continue

        log(f"calling {company} {e164}{tag} …")
        try:
            if l.engine == "vapi":
                cid = V.dispatch(company, e164, webhook_url(), WEBHOOK_SECRET)
                if webhook_mode():
                    log(f"dispatched {company} · awaiting report")
                else:
                    call = V.fetch(cid)
                    rec = {"company": company, "phone": e164, "call_id": cid,
                           "status": call.get("status", ""), "ended_reason": call.get("endedReason", ""),
                           "cost_usd": call.get("cost", ""), **V.extract(call)}
                    store.append("results", rec)
                    V.write_result(rec, DATA / "results_vapi.csv")
                    log(f"done {company} · status={rec.get('status')} · meeting={rec.get('meeting_booked','')}")
            else:
                resp = B.send_call(B.build_task(company, e164))
                cid = resp.get("call_id", "")
                if not webhook_mode():
                    call = B.poll_call(cid)
                    answers = B.analyze_call(cid) if call.get("completed") else []
                    rec = B.result_row(company, e164, {**call, "call_id": cid}, answers)
                    store.append("results", rec)
                    B.write_result(rec, DATA / "results.csv")
                    log(f"done {company} · status={rec.get('status')}")
                else:
                    log(f"dispatched {company}")
        except Exception as e:
            log(f"ERROR {company}: {e}")
        placed += 1
        store.merge("campaign", {"placed": placed, "dispatched": placed})
        if not webhook_mode():
            time.sleep(V.CFG["seconds_between_calls"])

    if webhook_mode() and l.live:
        log(f"{placed} call(s) dispatched · results arrive as each call ends")
    else:
        log(f"campaign finished · {placed} {'planned' if not l.live else 'placed'}")
        store.merge("campaign", {"running": False})
    return {"ok": True, "dispatched": placed}


# ---------------- vapi webhook ----------------
@app.post("/api/webhook/vapi")
async def vapi_webhook(request: Request):
    """Vapi POSTs the end-of-call report here. One row per completed call."""
    if WEBHOOK_SECRET and request.headers.get("x-vapi-secret") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "bad secret"}, status_code=401)

    body = await request.json()
    msg = body.get("message", body) or {}
    if msg.get("type") not in (None, "end-of-call-report"):
        return {"ok": True, "ignored": msg.get("type")}

    call = msg.get("call") or {}
    analysis = msg.get("analysis") or call.get("analysis") or {}
    company = ((call.get("metadata") or {}).get("company")
               or (msg.get("assistant") or {}).get("name") or "—")
    phone = (call.get("customer") or {}).get("number", "")

    rec = {"company": company, "phone": phone, "call_id": call.get("id", ""),
           "status": msg.get("endedReason") or call.get("status", "ended"),
           "ended_reason": msg.get("endedReason", ""),
           "cost_usd": msg.get("cost", call.get("cost", "")),
           **V.extract({"analysis": analysis})}
    store.append("results", rec)
    log(f"report {company} · meeting={rec.get('meeting_booked','')}")

    c = store.get("campaign", {}) or {}
    if c.get("dispatched") and len(store.get("results", []) or []) >= c["dispatched"]:
        store.merge("campaign", {"running": False})
        log("campaign finished · all reports received")
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "mode": "webhook" if webhook_mode() else "polling",
            "store": store.backend()}


# ---------------- static frontend ----------------
STATIC = ROOT / "server" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC / "index.html"))
