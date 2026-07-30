# Compliance — read before you call anyone

This is not legal advice — it's a practical checklist so you run a clean, defensible
outbound program. When in doubt, talk to a lawyer, especially before scaling past a few
dozen calls. The code is built to help you follow these; don't remove the guardrails.

## The short version

You're doing **B2B research + appointment-setting**, calling **published business numbers**,
with an agent that **discloses it's an AI**, during **business hours**, and **honors opt-outs**.
That's the safe lane. You get into trouble by hiding the AI, calling cells/homes, spoofing
caller ID, ignoring "stop", or auto-dialing at spam scale.

## Rules the code already enforces

- **AI disclosure.** The script identifies the caller as an AI in the first sentences.
  Several states (e.g. California's "Bot Disclosure" law) require this for automated calls;
  it's also just the honest thing to do. **Keep it in.**
- **Recording consent.** `campaign.py` prepends a recording-notice line for numbers whose
  state is on the two-party-consent list. Recording is on by default; if you'd rather not
  record, set `RECORD=false` in `.env` and you can drop the notice. The built-in state list
  is a rough guess from area code — **verify the actual state** of a number before relying on it.
- **Calling hours.** Calls only go out **9am–5pm, weekdays, in the number's local timezone**
  (configurable). Federal telemarketing hours are 8am–9pm; business-hours is the polite, safe subset.
- **Do-not-call, honored two ways.** Any row flagged in the `do_not_call` column is skipped,
  and the post-call analysis captures whether the person asked to be removed so you can flag them.
- **Hard call-length cap** (`MAX_DURATION_MIN`) so nothing runs long.

## What YOU are responsible for

- [ ] **Only call business lines.** The seeded list is business numbers. Don't add personal cell
      numbers — calling cells with an autodialer/prerecorded-or-AI voice is where **TCPA** liability lives.
- [ ] **Business-to-business is generally exempt from the national DNC registry**, but some states
      have their own rules. If you add consumer/residential numbers, scrub them against the
      **National Do Not Call Registry** first. Don't call anyone who's told you to stop.
- [ ] **Use a real, accurate caller ID** (`FROM_NUMBER`) that can receive callbacks. Never spoof.
- [ ] **Verify recording law per call/state.** The built-in two-party list is a starting point,
      not gospel. If you're unsure, either don't record or always play the notice (`RECORD=false`
      or force the consent line).
- [ ] **Honor opt-outs immediately and permanently** — set `do_not_call` for anyone who asks, and
      don't re-run them.
- [ ] **Keep volumes sane.** Pause between calls (default 8s), keep batches modest, and don't
      hammer the same number. Spam-scale auto-dialing gets numbers flagged and invites complaints.
- [ ] **Check state AI-calling laws.** A handful of states are adding specific rules for AI/synthetic
      voice in outbound calls. Quick check before a big push.

## A good first run

1. Put **only your own cell** in a test `targets.csv` and run `--live --limit 1`. Listen to the
   whole thing. Tune the script in `caller/script.py`.
2. Run `--live --limit 3` against three friendly business numbers. Read the transcripts.
3. Then open it up — in modest batches, during business hours, watching `results.csv`.

## If someone's unhappy

Have the agent apologize once, offer to email instead, and flag `do_not_call`. A calm,
honest, easy-to-opt-out-of call to a business about software they might actually want is
low-risk. Keep it that way.
