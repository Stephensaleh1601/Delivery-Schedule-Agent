# Delivery-Schedule-Agent

Dispatch sequencing agent for a Singapore large-furniture delivery company (sofas, beds,
cabinets). A customer messages a WhatsApp-style chat, an agent (or, for the chat's own booking
form, a plain structured submit) turns it into a job record, a second agent works out where it
fits in the day's route, and the coordinator approves, edits or rejects the proposed schedule
before anything goes to a customer. See `PRD.md` for the original brief; this README covers
what's actually built and how to run it.

Built for IGNITE Agentic AI Hackathon 2026, digital track.

## What's built

- **Intake agent** (`dispatch_agent/agents/intake_agent.py`) — a LangGraph graph that reads a
  WhatsApp message, extracts a structured job (name, address, postal code, job type,
  availability) via a forced Claude tool call, geocodes the postal code, and persists it.
  Missing fields (no postal code, no name) come back as `errors` instead of a guess.
- **Planning agent** (`dispatch_agent/agents/planning_agent.py`) — takes a day's jobs, gets a
  real drive-time matrix, solves an exact single-vehicle time-windowed sequence with OR-Tools,
  and drafts a short WhatsApp confirmation per stop.
- **Reschedule loop** (`dispatch_agent/reschedule.py`) — a customer moves, the day re-solves,
  and only the customers whose arrival window actually changed come back as "affected".
- **Web app** (`dispatch_agent/webapp/`) — FastAPI, plain HTML/JS pages, no build step:
  - **Client front face** (`/`) — a WhatsApp-look chat UI (`static/client.html`, `chat.css`,
    `chat.js`). A rule-based conversation engine (`dispatch_agent/webapp/chat.py`, no LLM call)
    greets the customer with two options: book a delivery (bot replies with an inline card-style
    form for name/address/furniture type/preferred date+time) or reschedule an existing one
    (bot asks for name/phone, looks the booking up, then negotiates a new slot -- it calls the
    real solver via `apply_reschedule`, and if the requested slot doesn't fit the rest of that
    day it asks the customer to suggest a different one, up to a few tries before handing off to
    the team).
  - **Back office** (`/admin`) — lists incoming orders and has a "Generate Route Plan" button
    showing sequence, scheduled arrival, and distance/duration from the previous stop for each
    client, plus an interactive Google map (`/api/config` hands the browser the Maps JS key +
    depot) pinning every stop in order with the actual road-following route (Directions API,
    falling back to a straight line if that API isn't reachable/enabled). Polls for
    notifications every 15s -- if a chat reschedule touched a date whose plan was already
    generated, a banner appears with a one-click "Regenerate" per affected date. A "Share Trip
    Link" button on the generated plan builds a plain `google.com/maps/dir/...` deep link
    (Google's public URL scheme, no API key or OAuth) through every stop in the solved order --
    send it to a driver's phone and it opens turn-by-turn navigation in their own Google Maps
    app, signed into their own account, no "connect an account" step needed.
- **Dashboard** (`dispatch_agent/dashboard/app.py`) — the original Streamlit prototype: a map,
  approve/edit/reject per stop, and a reschedule trigger, with every edit logged to the override
  table. Superseded by the web app above for day-to-day use; kept because the
  approve/edit/reject + reschedule flow isn't in the web app yet.
- **Solver** (`dispatch_agent/solver.py`) — single-vehicle TSP with time windows via OR-Tools'
  routing library. Exact, not a heuristic, which is fine at the size this runs at (a handful to
  a few dozen jobs/day).
- **Storage** (`dispatch_agent/db.py`) — SQLite: jobs, the day's proposed sequence, and the
  coordinator override log.

## What's a placeholder

Three files are meant to be swapped for the real ones the PRD calls "reused from Stow" — this
repo doesn't have that source, so each ships with a working but coarse fallback and a comment
saying so:

- `dispatch_agent/geo/postal_codes.py` — Singapore's 28 postal districts, not exact addresses.
- `dispatch_agent/geo/zones.py` — same district centroids, used for zone lookups.
- `dispatch_agent/geo/routing_client.py` — Google Maps Distance Matrix API by default, OneMap
  (SG's free routing API, with the Johor-route filter the PRD flags) as an alternative, falling
  back to a haversine estimate with no credentials or network for either. This fallback is also
  what makes the test suite and `run_demo.py` runnable offline.

AgentCore deployment isn't wired up — the PRD lists it as the target runtime, but doing that
config against a real AWS account is out of scope for what can be verified in this repo. The
dashboard and demo script run locally against the same `dispatch_agent` package that would be
deployed.

## Repo layout

```
dispatch_agent/
  models.py            Pydantic schemas -- the shape everything else agrees on
  config.py             env-driven settings
  db.py                  SQLite: jobs, day sequences, override log
  llm.py                  Bedrock and OpenAI clients (free text + forced-tool-call structured output),
                            picked by LLM_PROVIDER via build_llm_client()
  solver.py               OR-Tools single-vehicle TSP with time windows
  reschedule.py            re-solve + diff affected customers on a reschedule
  agents/
    prompts.py             prompt text and the intake tool's JSON schema
    intake_agent.py         LangGraph: message -> validated, geocoded JobRecord
    planning_agent.py        LangGraph: jobs -> sequence + drafted messages
  geo/
    postal_codes.py          postal code -> coordinates (placeholder, see above)
    zones.py                   zone centroids (placeholder, see above)
    routing_client.py           drive time + distance: Google Distance Matrix, OneMap, or haversine
  webapp/
    main.py                      FastAPI: /api/jobs, /api/chat, back-office route-plan API
    jobs_service.py                shared job-creation logic (used by /api/jobs and chat.py)
    chat.py                         rule-based conversation engine: booking form + reschedule negotiation
    static/
      client.html, chat.css, chat.js    WhatsApp-look chat UI (front face)
      admin.html, admin.js, style.css    back-office dashboard (route plan + map)
  dashboard/
    app.py                      Streamlit prototype -- map + approve/edit/reject + reschedule
scripts/
  seed_db.py                     create the SQLite schema
  seed_test_clients.py             wipe + seed 20 synthetic furniture-delivery clients across 5 dates
  run_demo.py                     the PRD's demo beat end to end
data/
  sample_messages.json             3 sample WhatsApp messages for the demo (furniture delivery)
tests/
  conftest.py                       temp-DB fixture + a FakeLLM (no network in tests)
  test_models.py, test_solver.py, test_intake_agent.py, test_reschedule.py
```

## How to run it

Requires Python 3.11+.

```bash
pip install -r requirements.txt
pip install -e .          # makes `dispatch_agent` importable everywhere
cp .env.example .env
python scripts/seed_db.py
```

### Setup checklist -- APIs and credentials required

This project needs credentials for two *independent* things, each with a choice of provider.
Pick one option per row, put the matching values in `.env`, and leave the other option's fields
blank.

| Used for | Pick one | Set in `.env` |
|---|---|---|
| LLM (intake + planning agents) | AWS Bedrock **or** OpenAI | `LLM_PROVIDER=bedrock` or `openai` |
| Routing + admin map (drive time/distance, route map) | Google Maps **or** OneMap **or** neither (haversine fallback, no signup) | `ROUTING_PROVIDER=google`, `onemap`, or `haversine` |

Nothing else needs a cloud account: `pytest` and everything in `dashboard/` and `webapp/` runs
against SQLite locally, and the haversine fallback keeps routing working with zero credentials.

> **If you're using Google Maps, there are two separate APIs to switch on for the same key --
> missing either one is the most common reason things silently fall back or the map shows an
> error.** See "Option A: Google Maps" under section 2 below.

### 1. LLM provider (needed for anything that calls an LLM, i.e. everything except `pytest`)

Set `LLM_PROVIDER` in `.env` to `bedrock` (default) or `openai`. Only the section for the one
you pick needs real credentials.

#### Option A: AWS Bedrock (`LLM_PROVIDER=bedrock`)

Nothing in this repo has an AWS-console step for you to click through except this one. Set
both of these before the agents will actually call Bedrock:

1. **Get credentials into `.env`.** Open `.env` and pick one of the two options already
   documented there:
   - Already have `aws configure` set up? Set `AWS_PROFILE` to that profile's name.
   - No AWS CLI? Create an access key in the AWS Console under **IAM → Users → your user →
     Security credentials → Create access key**, then paste the key id and secret into
     `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in `.env`. No CLI install needed — boto3
     reads them straight from the environment.
2. **Request Bedrock model access.** In the AWS Console, go to **Bedrock → Model access** (in
   the same region as `AWS_REGION`, default `ap-southeast-1`) and enable Claude Haiku. This is
   a one-time, per-account approval step — valid credentials alone aren't enough, Bedrock
   blocks calls to models you haven't been granted access to.
3. **Confirm the model id.** `BEDROCK_MODEL_ID` in `.env` is a placeholder — check the exact id
   string in the Bedrock console's model catalog (or `aws bedrock list-foundation-models`) for
   your region and update it if it doesn't match.

#### Option B: OpenAI (`LLM_PROVIDER=openai`)

Set `OPENAI_API_KEY` in `.env` to a key from platform.openai.com/api-keys, and optionally
`OPENAI_MODEL` (default `gpt-4o-mini`). No AWS setup needed for this path — `build_llm_client()`
in `dispatch_agent/llm.py` returns `OpenAIChat` instead of `BedrockClaude`, same
`.complete()` / `.extract_structured()` interface, so nothing else in the agents changes.

### 2. Routing provider + admin map (Google Maps or OneMap)

Set `ROUTING_PROVIDER` in `.env` to `google` (default), `onemap`, or `haversine`.

#### Option A: Google Maps (`ROUTING_PROVIDER=google`)

Get a plain Maps API key (Google Cloud Console → **APIs & Services → Credentials → Create
credentials → API key**) and set `GOOGLE_MAPS_API_KEY` in `.env`. This is a normal API key, not
a GCP service account — Google's separate *Route Optimization API* needs the latter and isn't
what this project uses.

> **⚠️ Enable all three of these APIs on that key — Google Cloud Console → APIs & Services →
> Library — or things will silently misbehave instead of erroring clearly:**
>
> 1. **Distance Matrix API** — used by `dispatch_agent/geo/routing_client.py` for drive
>    time/distance between stops (one batched request per day's stop list, not one per pair).
>    Missing this: routing quietly falls back to a straight-line haversine estimate (no crash,
>    just less accurate times/distances).
> 2. **Maps JavaScript API** — used by the admin dashboard's interactive route map
>    (`/admin`). Missing this: the map area shows an authentication error instead of rendering
>    (the page itself still loads fine).
> 3. **Directions API** — used by the same map to draw the actual road-following route between
>    stops. Missing this: the map still renders with numbered stop markers, but the connecting
>    line is a straight-line estimate instead of the real driving path (a note under the map
>    says so when this happens).
>
> Each is a separate on/off switch even though all three use the same key — enabling one does
> not enable the others. Search each API by name in the Library and click **Enable**.
>
> The key is sent to the browser as-is for the map — that's Google's own design for the Maps
> JavaScript API, not a mistake in this codebase. Restrict the key by **HTTP referrer** in Cloud
> Console (APIs & Services → Credentials → your key → Application restrictions) rather than
> relying on it staying secret, especially before deploying this anywhere public.

#### Option B: OneMap (`ROUTING_PROVIDER=onemap`)

Free at onemap.gov.sg, Singapore-only. Set `ONEMAP_TOKEN` (a pre-issued token, takes priority)
or `ONEMAP_EMAIL`/`ONEMAP_PASSWORD` (exchanged for a token automatically). No admin-dashboard map
in this mode — that only needs `GOOGLE_MAPS_API_KEY` (Maps JavaScript API enabled), independent
of which `ROUTING_PROVIDER` you picked for the actual drive-time solving. Set both if you want
OneMap routing with the Google map.

#### Option C: no credentials (`ROUTING_PROVIDER=haversine`, or just leave the above blank)

Falls back to a straight-line distance estimate automatically. This is also what keeps the test
suite and `run_demo.py` runnable completely offline.

### 3. Depot

Every route starts and ends at the company office: **8 Somapah Rd, Singapore 487372 (SUTD)**,
`dispatch_agent/geo/zones.py`'s `COMPANY_DEPOT` (rooftop-precision coordinates, resolved once via
the Google Geocoding API). Change that constant if the office ever moves. Nothing to configure
in `.env` for this — it's a code constant, not a credential.

### Test data

`scripts/seed_test_clients.py` inserts 20 synthetic furniture customers across 5 weekdays -- 7
on the first date, spread across 7 genuinely different Singapore neighbourhoods (Tampines,
Bedok, Katong, Geylang, Macpherson, Toa Payoh, Hougang) rather than clustered next to the
depot, and 13 more spread even further (including the far west and north) across the other 4
dates at 3-4/day. No LLM call, no network, so it always works.

The busy date is capped at 7, not more: 10 same-day sofa deliveries (45 min each) only leave 90
minutes of total travel budget in a 09:00-18:00 day, and real drive times between distinct
neighbourhoods (11-30+ min each) blow through that in a handful of legs -- verified against the
live solver with a 30-second search budget, not just infeasible-by-guess. 7 stops (225 minutes
of travel budget) comfortably fits this spread.

**⚠️ This wipes every job, sequence, and notification currently in the DB** -- not just
previously-seeded ones. Anything booked through the live chat or admin dashboard, including
your own manual testing, is deleted too. Only run this against a database nobody's actively
using.

```bash
python scripts/seed_test_clients.py
```

### Testing

```bash
# 1. Fully offline -- no cloud credentials, no network, no .env needed. Exercises the models,
#    the OR-Tools solver, the intake/reschedule logic, and SQLite persistence with a FakeLLM
#    standing in for the LLM.
python -m pytest

# 2. End-to-end with real LLM calls -- needs the LLM provider credentials above.
#    Runs the PRD's demo beat: 3 WhatsApp messages -> intake -> sequence -> approve ->
#    reschedule -> re-sequence, printing each step to the terminal.
python scripts/run_demo.py

# 3. Interactive -- the web app. Needs jobs in the DB: run scripts/seed_test_clients.py, run
#    step 2 above, book one through the chat, or use the Streamlit dashboard below.
uvicorn dispatch_agent.webapp.main:app --reload
#   -> http://localhost:8000/       WhatsApp-style client chat (public) -- book or reschedule
#   -> http://localhost:8000/admin  back-office dashboard: orders + "Generate Route Plan"

# 3b. The original Streamlit prototype -- map view, approve/edit/reject, reschedule trigger.
streamlit run dispatch_agent/dashboard/app.py
```

If step 2 fails immediately, it's almost always one of: credentials not picked up (check
`.env` was actually loaded — `python -c "from dispatch_agent.config import settings; print(settings.llm_provider)"`),
or (Bedrock specifically) model access not yet granted / a stale `BEDROCK_MODEL_ID`.

**Neither the client chat's booking form nor its reschedule negotiation calls an LLM** -- both
are plain structured flows (see `dispatch_agent/webapp/chat.py`'s module docstring), so they
work regardless of `LLM_PROVIDER`. Under the hood, reschedule calls `apply_reschedule(...,
draft_messages=False)`, which re-solves the day but skips the planning agent's drafted WhatsApp
messages (the chat sends its own confirmation instead) -- `scripts/run_demo.py` and the
Streamlit dashboard still draft real messages via the LLM, since they call it with the default
`draft_messages=True`.

### Notifications: a chat reschedule making an already-generated route plan stale

If a customer reschedules via the chat after the admin already clicked "Generate Route Plan"
for the affected date(s), that plan is now out of date -- the job composition changed but the
saved sequence didn't. `dispatch_agent/webapp/chat.py` detects this (by checking whether the
old and/or new date already had a saved `DaySequence` before the move) and writes a
`Notification` row. The admin dashboard polls `GET /api/notifications` every 15s and shows a
banner with the affected date(s) and a one-click "Regenerate `<date>`" button per date, plus a
"Dismiss" button (`POST /api/notifications/{id}/dismiss`). This is independent of whether the
LLM drafts a message -- it's driven purely by whether a sequence already existed.

### Troubleshooting: SSL certificate errors calling Google/OneMap

On some networks (a corporate proxy or VPN doing TLS interception, some antivirus/endpoint
security software) the OS trusts the intercepting certificate but Python's bundled `certifi` CA
list doesn't, so every `requests` call to Google Maps or OneMap fails with
`CERTIFICATE_VERIFY_FAILED`. The routing client's broad fallback handling means this doesn't
crash anything -- it just silently degrades every drive-time/distance call to the haversine
estimate (and, for the batched Distance Matrix call, still pays for every failed connection
attempt first, so it's also slow). `dispatch_agent/config.py` calls
`truststore.inject_into_ssl()` at import time to use the OS's own trust store instead of
`certifi`'s, which fixes this on Windows/macOS/Linux without needing to install a corporate root
CA into Python manually. If you still see certificate errors after `pip install -r
requirements.txt`, confirm `truststore` actually installed (`python -c "import truststore"`).

## How this maps to the PRD's three demo numbers

- *How often the intake agent produces a usable job record first try* — count `errors == []`
  results from `run_intake` / `scripts/run_demo.py` against a labelled message set.
- *How often a message goes to an approved slot with no human edit* — count jobs approved
  as-is vs. rows in `override_log` for that delivery date (`JobsRepository.overrides_for_date`).
- *Total drive time of the agent's day against the same day sequenced by hand* — compare
  `DaySequence.total_drive_minutes` against a hand-sequenced run of the same job list.

None of these are wired up as a reporting script yet — the data (`override_log`, saved
sequences) is there; building the actual slide numbers is left to whoever runs the comparison
against a real hand-sequenced day, since that's the part the PRD says needs a real person.
