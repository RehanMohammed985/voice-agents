#!/usr/bin/env python3
"""
vapi.py — the same outbound research campaign, powered by Vapi (https://vapi.ai).

WHY VAPI FOR YOU
----------------
- Your $100 (+$250 demo bonus) Vapi credit covers the platform.
- Vapi lets you BRING YOUR OWN DEEPGRAM KEY for transcription — add it under
  Dashboard -> Providers/Credentials -> Deepgram, and every call's STT bills to
  your $15,000 Deepgram credit instead of your Vapi balance.
- Vapi gives you a FREE US phone number (Dashboard -> Phone Numbers -> Create).

SETUP (~5 min, mostly clicks)
-----------------------------
1. vapi.ai -> sign in -> copy your PRIVATE API key   -> VAPI_API_KEY in .env
2. Dashboard -> Phone Numbers -> Create (free number) -> VAPI_PHONE_NUMBER_ID in .env
3. (Recommended) Dashboard -> Providers -> add your Deepgram API key.
4. python -m caller.vapi            # dry run, $0
   python -m caller.vapi --live --limit 1   # call your own cell first

No dashboard "assistant" needed — this runner sends a TRANSIENT assistant inline
with every call, built from caller/script.py, including a structured-data schema
so answers come back as columns in data/results_vapi.csv.
"""
import argparse, csv, datetime as dt, os, re, sys, time
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None
import requests
try:
    from dotenv import load_dotenv; load_dotenv()
except Exception:
    pass
from caller import script as S

API = "https://api.vapi.ai"
ROOT = Path(__file__).resolve().parent.parent

CFG = {
    "api_key": os.getenv("VAPI_API_KEY", ""),
    "phone_number_id": os.getenv("VAPI_PHONE_NUMBER_ID", ""),
    "model_provider": os.getenv("VAPI_MODEL_PROVIDER", "openai"),
    "model": os.getenv("VAPI_MODEL", "gpt-4o-mini"),
    "voice_provider": os.getenv("VAPI_VOICE_PROVIDER", "vapi"),
    "voice": os.getenv("VAPI_VOICE", "Elliot"),
    "max_duration_s": int(os.getenv("MAX_DURATION_MIN", "6")) * 60,
    "call_window_start": int(os.getenv("CALL_WINDOW_START", "9")),
    "call_window_end": int(os.getenv("CALL_WINDOW_END", "17")),
    "seconds_between_calls": float(os.getenv("SECONDS_BETWEEN_CALLS", "8")),
    "record": os.getenv("RECORD", "true").lower() == "true",
}

TWO_PARTY_STATES = {"CA","DE","FL","IL","MD","MA","MI","MT","NH","OR","PA","WA","CT","NV"}
AREACODE_TZ = {
    "206":"America/Los_Angeles","253":"America/Los_Angeles","360":"America/Los_Angeles","503":"America/Los_Angeles",
    "530":"America/Los_Angeles","619":"America/Los_Angeles","760":"America/Los_Angeles","805":"America/Los_Angeles",
    "916":"America/Los_Angeles","303":"America/Denver","480":"America/Phoenix","505":"America/Denver","602":"America/Phoenix",
    "720":"America/Denver","801":"America/Denver","928":"America/Phoenix","217":"America/Chicago","312":"America/Chicago",
    "314":"America/Chicago","507":"America/Chicago","630":"America/Chicago","708":"America/Chicago","773":"America/Chicago",
    "847":"America/Chicago","920":"America/Chicago","972":"America/Chicago","713":"America/Chicago","352":"America/New_York",
    "203":"America/New_York","215":"America/New_York","267":"America/New_York","301":"America/New_York","302":"America/New_York",
    "404":"America/New_York","410":"America/New_York","443":"America/New_York","470":"America/New_York","518":"America/New_York",
    "610":"America/New_York","614":"America/New_York","617":"America/New_York","703":"America/New_York","704":"America/New_York",
    "732":"America/New_York","802":"America/New_York","843":"America/New_York","908":"America/New_York","914":"America/New_York",
    "917":"America/New_York","919":"America/New_York",
}
AREACODE_STATE = {"206":"WA","253":"WA","360":"WA","503":"OR","530":"CA","619":"CA","760":"CA","805":"CA","916":"CA",
    "312":"IL","217":"IL","630":"IL","708":"IL","773":"IL","847":"IL","617":"MA","215":"PA","267":"PA","610":"PA",
    "301":"MD","410":"MD","443":"MD","203":"CT","352":"FL"}

STRUCT_FIELDS = ["reached_relevant","person_name","person_title","multi_brand_fleet","reporting_method",
    "biggest_frustration","meeting_booked","meeting_time","followup_email","do_not_call","sentiment","summary"]


def norm_phone(raw):
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 10: d = "1" + d
    return "+" + d if d else ""
def ac(e): return e[2:5] if len(e) >= 5 else ""
def within_window(e, ignore):
    if ignore: return True, "hours ignored"
    n = AREACODE_TZ.get(ac(e), "America/New_York")
    now = dt.datetime.now(ZoneInfo(n)) if ZoneInfo else dt.datetime.now()
    if now.weekday() >= 5: return False, f"weekend in {n}"
    if not (CFG["call_window_start"] <= now.hour < CFG["call_window_end"]):
        return False, f"{now.strftime('%H:%M')} {n} outside window"
    return True, f"{now.strftime('%H:%M')} {n}"
def needs_consent(e): return AREACODE_STATE.get(ac(e), "") in TWO_PARTY_STATES


def assistant_for(company: str, consent: bool, server_url: str = "", server_secret: str = "") -> dict:
    """Build a transient Vapi assistant from caller/script.py."""
    task = S.render(S.TASK_PROMPT)
    first = ("Hi — quick note before we start: this call may be recorded for quality. "
             if consent else "") + (
        f"Hi, this is Ava — I'm an AI assistant calling on behalf of {S.CALLER['founder']}, "
        f"who's building software for solar asset management and O&M. Do you have about two "
        f"minutes? I'm doing research, not selling anything.")
    schema_props = {
        "reached_relevant": {"type": "boolean", "description": "Did we reach an asset-management / O&M / operations / monitoring person?"},
        "person_name": {"type": "string", "description": "Name of the person we spoke with, if stated."},
        "person_title": {"type": "string", "description": "Their role/title, if stated."},
        "multi_brand_fleet": {"type": "string", "description": "Do they run multiple inverter brands / monitoring portals? yes/no/unknown"},
        "reporting_method": {"type": "string", "description": "How they produce monthly performance/availability reporting and who owns it."},
        "biggest_frustration": {"type": "string", "description": "Most annoying part of monitoring/reporting across their fleet."},
        "meeting_booked": {"type": "boolean", "description": "Did they agree to a 20-minute meeting with the founder?"},
        "meeting_time": {"type": "string", "description": "Day/time mentioned for the meeting, if any."},
        "followup_email": {"type": "string", "description": "Email address given for follow-up, if any."},
        "do_not_call": {"type": "boolean", "description": "Did they ask to be removed / never called again?"},
        "sentiment": {"type": "string", "description": "positive / neutral / negative"},
        "summary": {"type": "string", "description": "One-sentence summary of the call."},
    }
    a = {
        "name": "Azimuth research (transient)",
        "firstMessage": first,
        "model": {
            "provider": CFG["model_provider"],
            "model": CFG["model"],
            "temperature": 0.7,
            "messages": [{"role": "system",
                          "content": task + f"\n\nThe company you are calling right now is: {company}."}],
        },
        "voice": {"provider": CFG["voice_provider"], "voiceId": CFG["voice"]},
        "transcriber": {"provider": "deepgram", "model": "nova-2"},  # bills to YOUR Deepgram key if added in dashboard
        "maxDurationSeconds": CFG["max_duration_s"],
        "voicemailDetection": {"provider": "twilio"},
        "voicemailMessage": S.render(S.VOICEMAIL_MESSAGE),
        "endCallMessage": "Thanks so much for the time — have a great day.",
        "analysisPlan": {
            "summaryPlan": {"enabled": True},
            "structuredDataPlan": {
                "enabled": True,
                "schema": {"type": "object", "properties": schema_props},
            },
        },
        "artifactPlan": {"recordingEnabled": CFG["record"]},
    }
    if server_url:
        # Vapi POSTs the end-of-call report here instead of us polling for it.
        a["server"] = {"url": server_url}
        if server_secret:
            a["server"]["secret"] = server_secret
        a["serverMessages"] = ["end-of-call-report"]
    return a


def dispatch(company, e164, server_url: str = "", server_secret: str = ""):
    """Place the call and return its id immediately. Does not wait for the call."""
    body = {
        "phoneNumberId": CFG["phone_number_id"],
        "customer": {"number": e164},
        "assistant": assistant_for(company, needs_consent(e164), server_url, server_secret),
        "metadata": {"company": company},
    }
    r = requests.post(f"{API}/call", headers={"Authorization": f"Bearer {CFG['api_key']}"},
                      json=body, timeout=30)
    r.raise_for_status()
    return r.json().get("id")


def fetch(call_id, timeout_s=600):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        r = requests.get(f"{API}/call/{call_id}",
                         headers={"Authorization": f"Bearer {CFG['api_key']}"}, timeout=30)
        r.raise_for_status()
        d = r.json()
        if d.get("status") in ("ended", "failed"):
            return d
        time.sleep(10)
    return {"status": "timeout"}


def extract(call):
    sd = ((call.get("analysis") or {}).get("structuredData")) or {}
    row = {f: sd.get(f, "") for f in STRUCT_FIELDS}
    if not row.get("summary"):
        row["summary"] = (call.get("analysis") or {}).get("summary", "")[:300]
    return row


def write_result(row, path):
    fields = ["company","phone","call_id","status","ended_reason","cost_usd"] + STRUCT_FIELDS
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new: w.writeheader()
        w.writerow(row)


def main():
    ap = argparse.ArgumentParser(description="Solar-facility calling campaign (Vapi).")
    ap.add_argument("--targets", default=str(ROOT / "data" / "targets.csv"))
    ap.add_argument("--results", default=str(ROOT / "data" / "results_vapi.csv"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--live", action="store_true", help="ACTUALLY place calls. Default is dry-run.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ignore-hours", action="store_true")
    a = ap.parse_args()
    dry = not a.live

    if a.live:
        missing = [n for n, k in (("VAPI_API_KEY","api_key"), ("VAPI_PHONE_NUMBER_ID","phone_number_id")) if not CFG[k]]
        if missing:
            sys.exit("ERROR: --live needs " + ", ".join(missing) + " in .env "
                     "(key: vapi.ai dashboard; number: Phone Numbers -> Create, it's free).")

    with open(a.targets, newline="", encoding="utf-8") as f:
        targets = list(csv.DictReader(f))
    for r in targets: r["_phone"] = norm_phone(r.get("phone", ""))

    print(f"\n{'='*68}\n  Azimuth outbound agent · Vapi · {'DRY RUN (no calls)' if dry else 'LIVE'}")
    print(f"  {len(targets)} rows · window {CFG['call_window_start']:02d}:00-{CFG['call_window_end']:02d}:00 local, weekdays"
          f" · model={CFG['model']} · voice={CFG['voice_provider']}/{CFG['voice']}\n{'='*68}\n")

    placed = 0
    for row in targets:
        company, e164 = row.get("company","").strip(), row["_phone"]
        if not e164:
            print(f"  skip  {company:<32} — no phone"); continue
        if str(row.get("do_not_call","")).strip().lower() in ("1","true","yes","y"):
            print(f"  skip  {company:<32} — do_not_call"); continue
        ok, why = within_window(e164, a.ignore_hours)
        tag = " [consent]" if needs_consent(e164) else ""
        if not ok:
            print(f"  hold  {company:<32} {e164} — {why}"); continue
        if a.limit and placed >= a.limit:
            print(f"\n  reached --limit {a.limit}."); break

        if dry:
            print(f"  WOULD CALL  {company:<30} {e164}  ({why}){tag}"); placed += 1; continue

        print(f"  calling  {company:<30} {e164}{tag} ...", flush=True)
        try:
            cid = dispatch(company, e164)
            if not cid:
                print("    ! no call id"); continue
            call = fetch(cid)
            data = extract(call)
            write_result({"company":company,"phone":e164,"call_id":cid,"status":call.get("status",""),
                          "ended_reason":call.get("endedReason",""),"cost_usd":call.get("cost",""), **data},
                         Path(a.results))
            print(f"    done  status={call.get('status')}  reason={call.get('endedReason','')}"
                  f"  meeting={data.get('meeting_booked','')}  cost=${call.get('cost','?')}")
        except requests.HTTPError as e:
            print(f"    ! HTTP {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            print(f"    ! error: {e}")
        placed += 1
        time.sleep(CFG["seconds_between_calls"])

    print(f"\n  {'planned' if dry else 'placed'} {placed} call(s).")
    if dry: print("  DRY RUN — add VAPI_API_KEY + VAPI_PHONE_NUMBER_ID to .env, then --live.\n")
    else:   print(f"  Results in {a.results}\n")


if __name__ == "__main__":
    main()
