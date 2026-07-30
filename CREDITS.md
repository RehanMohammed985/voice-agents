# Running the whole campaign on your free credits

You have five credit pots. Two are calling platforms, one is a massive input into a
calling platform, and two aren't useful for this project. Here's the allocation that
gets you to **hundreds of calls for $0 cash**.

## The plan at a glance

| Credit | What it is | Use it for | Verdict |
|---|---|---|---|
| **Vapi — $100 (+$250 for a demo)** | Full voice-agent platform (like Bland) | **PRIMARY ENGINE.** Free US phone number included. Runner: `caller/vapi.py` | ✅ core |
| **Deepgram — $15,000** | Speech-to-text API | **Plug INTO Vapi.** Add your Deepgram key in Vapi → Providers; every call's transcription then bills to Deepgram credit, not your Vapi balance | ✅ cost-shift |
| **Bolna — 2,000 minutes** | Outbound voice-agent platform | **BACKUP / BULK.** ~650 three-minute calls, free. Recreate the same script in Bolna's dashboard if/when Vapi runs dry | ✅ reserve |
| **Sarvam — $1,000** | Indian-language voice/LLM stack | Not useful for US English cold calls. Keep for a future India play | ⏸ park |
| **sync. — $500 + 3 mo scale** | Lipsync / video generation | Not calling-related. BUT: use it to make the **demo video that earns the +$250 Vapi credit** | 🎬 side quest |

## Why Vapi first (the math)

Vapi bills a small platform fee per minute plus pass-through costs for the model,
voice, transcription, and telephony. Two of those you can push onto free pots:

- **Transcription → your Deepgram $15k.** Vapi lets you attach your own Deepgram API key
  (Dashboard → Providers/Credentials → Deepgram). The runner already requests
  `transcriber: deepgram/nova-2`, so once the key is attached this line item leaves your bill.
- **Phone number → free.** Vapi issues free US numbers (Dashboard → Phone Numbers → Create).
  No Twilio account needed.
- Model + voice: the runner defaults to `gpt-4o-mini` + Vapi's built-in voice — the cheapest
  sane combo (roughly $0.10–0.15/min all-in against your Vapi credit; check your dashboard's
  live per-call cost, which the runner also logs per call in `cost_usd`).

**Ballpark:** $100 ≈ 650–1,000 connected minutes ≈ **200–300+ answered 3-min calls** —
several times your entire 21-number target list, with credit to spare. Post a demo for the
extra $250 and you're at ~1,000 calls of headroom. Then there's Bolna's 2,000 free minutes
behind that. You will run out of solar companies before you run out of credits.

## Setup for the free stack (once, ~5 minutes)

1. **Vapi:** sign in → copy Private API key → `.env` `VAPI_API_KEY=`
2. **Free number:** Dashboard → Phone Numbers → Create → copy its ID → `.env` `VAPI_PHONE_NUMBER_ID=`
3. **Deepgram cost-shift:** Dashboard → Providers (Credentials) → add your **Deepgram API key**.
4. Run:
   ```bash
   python -m caller.vapi                    # dry run, $0
   python -m caller.vapi --live --limit 1   # call YOUR OWN cell first
   python -m caller.vapi --live             # the list
   ```
   Everything else (script, questions, guardrails, targets.csv) is shared with the other engines.
   Results land in `data/results_vapi.csv` — including Vapi's **actual cost per call** in `cost_usd`,
   so you can watch the credit burn precisely.

## The +$250 Vapi bonus (worth doing)

Vapi's offer is credit for **posting a demo** of what you built. You already have everything
for a killer 60-second clip: screen-record a live call from `results_vapi.csv` + the Azimuth
landing page, post it (X/LinkedIn) tagging Vapi. If you want lip-synced narration over it,
that's a legitimate use of your **sync.** credit. $250 ≈ another ~2,000 call-minutes.

## Bolna (the 2,000-minute reserve)

Bolna is dashboard-driven: create an agent, paste the same system prompt from
`caller/script.py`, pick voice + Deepgram STT (your key again), buy/link a number, and
upload `data/targets.csv` as a batch. No code needed — keep it as the overflow engine
rather than maintaining a third runner until you actually need it.

## Guardrails still apply

Free credits don't change COMPLIANCE.md: AI disclosure stays in the first message,
consent line in two-party states, business hours by timezone, honor do-not-call.
The Vapi runner enforces all of these identically to the Bland one.
