# Deploying to Vercel

The console runs fine on your laptop with zero setup. Putting it on Vercel takes two
extra pieces, because serverless functions can't do two things this app originally did:
keep state in memory between requests, and run a background thread after the response
is sent.

So on Vercel the app switches into **webhook mode** automatically:

| | local (`uvicorn`) | Vercel |
|---|---|---|
| state | JSON files in `data/` | Upstash Redis |
| how results arrive | a thread polls Vapi until each call ends | Vapi POSTs a report to `/api/webhook/vapi` |
| what triggers it | nothing — the default | setting `PUBLIC_BASE_URL` |

Webhook mode is the better architecture anyway: dispatching 21 calls returns in about a
second instead of blocking for an hour, and results land as each call actually ends.

---

## 1. A Redis to hold state (2 min, free)

Vercel functions are stateless, so the call list, mission and results need somewhere to live.

1. [console.upstash.com](https://console.upstash.com) → **Create Database** → Redis → any region
2. Open the database, scroll to **REST API**, and copy `UPSTASH_REDIS_REST_URL` and
   `UPSTASH_REDIS_REST_TOKEN`

The REST API matters — plain `redis://` connections leak sockets on serverless. The app
uses the HTTP endpoint for exactly this reason.

## 2. Deploy (3 min)

```bash
npm i -g vercel
cd voice-agents
vercel            # first run: link/create the project
```

Note the URL it prints (`https://voice-agents-xxxx.vercel.app`), then set env vars —
either in the dashboard under **Settings → Environment Variables**, or:

```bash
vercel env add VAPI_API_KEY               production
vercel env add VAPI_PHONE_NUMBER_ID       production
vercel env add UPSTASH_REDIS_REST_URL     production
vercel env add UPSTASH_REDIS_REST_TOKEN   production
vercel env add PUBLIC_BASE_URL            production   # the URL above, no trailing slash
vercel env add WEBHOOK_SECRET             production   # any random string you invent
vercel --prod
```

`PUBLIC_BASE_URL` is the switch. Without it the app assumes it's on a real server and tries
to poll — which on Vercel means every campaign dies at the function timeout.

`WEBHOOK_SECRET` is optional but recommended: it's echoed by Vapi in the `x-vapi-secret`
header, and anything without it gets a 401. Otherwise your results endpoint is open to the
internet.

## 3. Check it

```
https://your-app.vercel.app/api/health
→ {"ok":true,"mode":"webhook","store":"upstash-redis"}
```

If it says `"mode":"polling"` your `PUBLIC_BASE_URL` didn't take. If it says
`"store":"local-files"`, the Upstash vars didn't — state will reset unpredictably
between requests.

Then run a **dry run** from the deployed UI before going live. It exercises the store,
the list, the timezone windows and the consent flags without spending a cent.

## 4. Your first live call

Put your own mobile in as the only target, set **stop after** to 1, flip LIVE, start.
Within a second the log says `dispatched · awaiting report`; when you hang up, Vapi POSTs
the report and the row appears in *What came back* with the real `cost_usd`.

---

## Notes and limits

**Bland on Vercel.** The webhook parser understands Vapi's report format. Bland calls will
dispatch but their results won't come back through this endpoint — use Vapi for the
deployed instance, or add a Bland webhook route.

**Big lists.** Dispatch is one HTTP request per number inside a single function
invocation, capped at 60 seconds — comfortably a few hundred numbers. Past that, split the
list or use the `limit` field.

**Local dev is unchanged.** No `PUBLIC_BASE_URL`, no Upstash vars → files and polling,
exactly as before. Nothing about deploying makes the local path worse.

**Testing the webhook locally.** Run `ngrok http 8000`, set `PUBLIC_BASE_URL` to the ngrok
URL, and the local server behaves like the deployed one.

## Other hosts

Anything that keeps a process alive (Railway, Render, Fly, a VPS) needs none of this —
`uvicorn server.app:app --host 0.0.0.0 --port $PORT` and the original polling mode works.
Setting `PUBLIC_BASE_URL` there still upgrades it to webhooks, which is worth doing.
