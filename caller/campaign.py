#!/usr/bin/env python3
"""
campaign.py — autonomous outbound calling campaign over a list of solar facilities.

Engine: Bland AI (https://bland.ai) — one API handles telephony + speech + the
conversational model. Swap-able (see README for the Vapi variant).

SAFETY BY DEFAULT
-----------------
- Runs in --dry-run mode unless you pass --live. Dry-run validates everything,
  prints the exact plan, and spends $0 / calls no one.
- Enforces a calling window (local business hours, weekdays) per target timezone.
- Skips any row flagged do_not_call.
- Plays a recording-consent line first in two-party-consent states.
- Discloses it is an AI at the top of every call (in the script).
Read COMPLIANCE.md before you pass --live.

USAGE
-----
    pip install -r requirements.txt
    cp .env.example .env         # then fill in BLAND_API_KEY etc.
    python -m caller.campaign --dry-run            # see the plan (default)
    python -m caller.campaign --live --limit 3     # actually call the first 3 eligible
    python -m caller.campaign --live               # run the whole eligible list
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py<3.9
    ZoneInfo = None

try:
    import requests
except ImportError:
    print("Missing dependency: pip install -r requirements.txt", file=sys.stderr)
    raise

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from caller import script as S  # noqa: E402

API = "https://api.bland.ai/v1"
ROOT = Path(__file__).resolve().parent.parent

# ---- config from env --------------------------------------------------------
CFG = {
    "api_key": os.getenv("BLAND_API_KEY", ""),
    "from_number": os.getenv("FROM_NUMBER", ""),          # optional; a Bland/your number
    "voice": os.getenv("VOICE", "june"),
    "max_duration_min": int(os.getenv("MAX_DURATION_MIN", "6")),
    "call_window_start": int(os.getenv("CALL_WINDOW_START", "9")),   # 9am local
    "call_window_end": int(os.getenv("CALL_WINDOW_END", "17")),      # 5pm local
    "seconds_between_calls": float(os.getenv("SECONDS_BETWEEN_CALLS", "8")),
    "record": os.getenv("RECORD", "true").lower() == "true",
    "webhook": os.getenv("WEBHOOK_URL", ""),
}

# States that require ALL parties to consent to recording. In these, we prepend a
# spoken consent line. (Verify current law before relying on this list.)
TWO_PARTY_STATES = {"CA","DE","FL","IL","MD","MA","MI","MT","NH","OR","PA","WA","CT","NV"}

# Minimal area-code -> IANA timezone map (extend as needed). Default: Eastern.
AREACODE_TZ = {
    # Pacific
    "206":"America/Los_Angeles","253":"America/Los_Angeles","360":"America/Los_Angeles","503":"America/Los_Angeles",
    "530":"America/Los_Angeles","619":"America/Los_Angeles","626":"America/Los_Angeles","714":"America/Los_Angeles",
    "760":"America/Los_Angeles","805":"America/Los_Angeles","818":"America/Los_Angeles","916":"America/Los_Angeles",
    # Mountain
    "303":"America/Denver","480":"America/Phoenix","505":"America/Denver","602":"America/Phoenix","720":"America/Denver",
    "801":"America/Denver","928":"America/Phoenix",
    # Central
    "217":"America/Chicago","312":"America/Chicago","314":"America/Chicago","507":"America/Chicago","630":"America/Chicago",
    "708":"America/Chicago","773":"America/Chicago","847":"America/Chicago","920":"America/Chicago","972":"America/Chicago",
    "352":"America/New_York",  # FL panhandle mostly ET
    # Eastern (explicit)
    "203":"America/New_York","215":"America/New_York","267":"America/New_York","301":"America/New_York",
    "302":"America/New_York","404":"America/New_York","410":"America/New_York","443":"America/New_York",
    "470":"America/New_York","518":"America/New_York","610":"America/New_York","614":"America/New_York",
    "617":"America/New_York","703":"America/New_York","704":"America/New_York","713":"America/Chicago",
    "732":"America/New_York","802":"America/New_York","843":"America/New_York","908":"America/New_York",
    "914":"America/New_York","917":"America/New_York","919":"America/New_York",
}
# state guess from area code, for two-party consent (rough; extend/verify)
AREACODE_STATE = {
    "206":"WA","253":"WA","360":"WA","503":"OR","530":"CA","619":"CA","626":"CA","714":"CA","760":"CA","805":"CA",
    "818":"CA","916":"CA","312":"IL","217":"IL","630":"IL","708":"IL","773":"IL","847":"IL","617":"MA","508":"MA",
    "215":"PA","267":"PA","610":"PA","301":"MD","410":"MD","443":"MD","203":"CT","860":"CT","352":"FL","305":"FL",
}


def norm_phone(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 10:
        d = "1" + d
    return "+" + d if d else ""


def area_code(e164: str) -> str:
    return e164[2:5] if len(e164) >= 5 else ""


def tz_for(e164: str):
    ac = area_code(e164)
    name = AREACODE_TZ.get(ac, "America/New_York")
    return ZoneInfo(name) if ZoneInfo else None, name


def within_window(e164: str) -> tuple[bool, str]:
    tz, name = tz_for(e164)
    now = dt.datetime.now(tz) if tz else dt.datetime.now()
    if now.weekday() >= 5:
        return False, f"weekend in {name}"
    if not (CFG["call_window_start"] <= now.hour < CFG["call_window_end"]):
        return False, f"{now.strftime('%H:%M')} {name} outside {CFG['call_window_start']}:00-{CFG['call_window_end']}:00"
    return True, f"{now.strftime('%H:%M')} {name}"


def needs_consent_line(e164: str) -> bool:
    return AREACODE_STATE.get(area_code(e164), "") in TWO_PARTY_STATES


def load_targets(path: Path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["_phone"] = norm_phone(r.get("phone", ""))
            rows.append(r)
    return rows


def build_task(company: str, e164: str) -> dict:
    task = S.render(S.TASK_PROMPT).replace("{company_name}", company)
    # personalize the one placeholder for the called org
    task = task.replace("your team", f"the team at {company}") if company else task
    payload = {
        "phone_number": e164,
        "task": task,
        "voice": CFG["voice"],
        "wait_for_greeting": True,
        "record": CFG["record"],
        "max_duration": CFG["max_duration_min"],
        "answered_by_enabled": True,           # answering-machine detection
        "voicemail_action": "leave_message",
        "voicemail_message": S.render(S.VOICEMAIL_MESSAGE),
        "temperature": 0.7,
        "metadata": {"company": company},
    }
    if CFG["from_number"]:
        payload["from"] = CFG["from_number"]
    if CFG["webhook"]:
        payload["webhook"] = CFG["webhook"]
    if needs_consent_line(e164):
        payload["first_sentence"] = (
            "Hi — quick note before we start: this call may be recorded for quality. "
        )
        payload["metadata"]["consent_state"] = True
    return payload


def send_call(payload: dict) -> dict:
    r = requests.post(f"{API}/calls", headers={"authorization": CFG["api_key"]},
                      json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def poll_call(call_id: str, timeout_s=600) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        r = requests.get(f"{API}/calls/{call_id}", headers={"authorization": CFG["api_key"]}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("completed") or data.get("status") in ("completed", "failed"):
            return data
        time.sleep(10)
    return {"completed": False, "status": "timeout"}


def analyze_call(call_id: str) -> list:
    body = {"goal": S.ANALYSIS_GOAL, "questions": [list(q) for q in S.ANALYSIS_QUESTIONS]}
    try:
        r = requests.post(f"{API}/calls/{call_id}/analyze", headers={"authorization": CFG["api_key"]},
                          json=body, timeout=60)
        r.raise_for_status()
        return r.json().get("answers", [])
    except Exception as e:
        print(f"    ! analyze failed: {e}")
        return []


def result_row(company, phone, call, answers) -> dict:
    a = list(answers) + [""] * (len(S.ANALYSIS_QUESTIONS) - len(answers))
    return {
        "company": company, "phone": phone,
        "call_id": call.get("call_id", ""),
        "status": call.get("status", ""),
        "answered_by": call.get("answered_by", ""),
        "call_length_min": call.get("call_length", ""),
        "reached_relevant": a[0], "person_name": a[1], "person_title": a[2],
        "multi_brand_fleet": a[3], "reporting_method": a[4], "biggest_frustration": a[5],
        "meeting_booked": a[6], "meeting_time": a[7], "followup_email": a[8],
        "do_not_call": a[9], "sentiment": a[10], "summary": a[11],
    }


def write_result(row: dict, out_path: Path):
    new = not out_path.exists()
    with open(out_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=S.RESULT_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def main():
    ap = argparse.ArgumentParser(description="Autonomous solar-facility calling campaign (Bland AI).")
    ap.add_argument("--targets", default=str(ROOT / "data" / "targets.csv"))
    ap.add_argument("--results", default=str(ROOT / "data" / "results.csv"))
    ap.add_argument("--limit", type=int, default=0, help="max calls this run (0 = all eligible)")
    ap.add_argument("--live", action="store_true", help="ACTUALLY place calls (costs money). Default is dry-run.")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry run (the default; validates + prints plan)")
    ap.add_argument("--ignore-hours", action="store_true", help="skip the calling-window check (testing only)")
    args = ap.parse_args()

    dry = not args.live
    if args.live and not CFG["api_key"]:
        sys.exit("ERROR: --live requires BLAND_API_KEY in your environment/.env")

    targets = load_targets(Path(args.targets))
    print(f"\n{'='*68}\n  Azimuth outbound research agent  —  {'DRY RUN (no calls)' if dry else 'LIVE'}")
    print(f"  {len(targets)} rows loaded from {args.targets}")
    print(f"  window {CFG['call_window_start']:02d}:00-{CFG['call_window_end']:02d}:00 local, weekdays  |  voice={CFG['voice']}  |  max {CFG['max_duration_min']}min\n{'='*68}\n")

    placed = 0
    for row in targets:
        company = row.get("company", "").strip()
        e164 = row.get("_phone", "")
        if not e164:
            print(f"  skip  {company:<34} — no valid phone")
            continue
        if str(row.get("do_not_call", "")).strip().lower() in ("1", "true", "yes", "y"):
            print(f"  skip  {company:<34} — do_not_call flag")
            continue
        ok, why = (True, "hours ignored") if args.ignore_hours else within_window(e164)
        consent = " [consent-line]" if needs_consent_line(e164) else ""
        if not ok:
            print(f"  hold  {company:<34} {e164}  — {why}")
            continue
        if args.limit and placed >= args.limit:
            print(f"\n  reached --limit {args.limit}; stopping.")
            break

        payload = build_task(company, e164)
        if dry:
            print(f"  WOULD CALL  {company:<30} {e164}  ({why}){consent}")
            placed += 1
            continue

        print(f"  calling  {company:<30} {e164}{consent} ...", flush=True)
        try:
            resp = send_call(payload)
            cid = resp.get("call_id")
            if not cid:
                print(f"    ! no call_id: {resp}")
                continue
            call = poll_call(cid)
            answers = analyze_call(cid) if call.get("completed") else []
            write_result(result_row(company, e164, {**call, "call_id": cid}, answers), Path(args.results))
            booked = (answers[6] if len(answers) > 6 else "")
            print(f"    done  status={call.get('status')}  answered_by={call.get('answered_by')}  meeting={booked}")
        except requests.HTTPError as e:
            print(f"    ! HTTP {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            print(f"    ! error: {e}")
        placed += 1
        time.sleep(CFG["seconds_between_calls"])

    print(f"\n  {'planned' if dry else 'placed'} {placed} call(s).")
    if dry:
        print("  This was a DRY RUN. Re-run with --live (and a funded Bland key) to actually call.\n")
    else:
        print(f"  Results appended to {args.results}\n")


if __name__ == "__main__":
    main()
