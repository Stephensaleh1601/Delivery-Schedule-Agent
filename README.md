# Delivery-Schedule-Agent

Dispatch sequencing agent for a Singapore home-installation company. A coordinator gets a
WhatsApp message, an agent turns it into a job record, a second agent works out where it fits
in the day's route, and the coordinator approves, edits or rejects the proposed schedule before
anything goes to a customer. See `PRD.md` (not in this repo — see the hackathon submission) for
the full brief; this README covers what's actually built and how to run it.

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
- **Dashboard** (`dispatch_agent/dashboard/app.py`) — Streamlit + a map. Coordinator sees the
  proposed day, approves/edits/rejects per stop, and triggers a reschedule. Every edit is
  logged to the override table.
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
- `dispatch_agent/geo/routing_client.py` — OneMap (SG's free routing API) with the Johor-route
  filter the PRD flags, falling back to a haversine estimate with no credentials or network.
  This fallback is also what makes the test suite and `run_demo.py` runnable offline.

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
  llm.py                  Bedrock Converse API wrapper (free text + forced-tool-call structured output)
  solver.py               OR-Tools single-vehicle TSP with time windows
  reschedule.py            re-solve + diff affected customers on a reschedule
  agents/
    prompts.py             prompt text and the intake tool's JSON schema
    intake_agent.py         LangGraph: message -> validated, geocoded JobRecord
    planning_agent.py        LangGraph: jobs -> sequence + drafted messages
  geo/
    postal_codes.py          postal code -> coordinates (placeholder, see above)
    zones.py                   zone centroids (placeholder, see above)
    routing_client.py           drive times, OneMap + Johor filter + haversine fallback
  dashboard/
    app.py                      Streamlit map + approve/edit/reject + reschedule trigger
scripts/
  seed_db.py                     create the SQLite schema
  run_demo.py                     the PRD's demo beat end to end
data/
  sample_messages.json             3 sample WhatsApp messages for the demo
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

### AWS credentials (needed for anything that calls Claude, i.e. everything except `pytest`)

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

OneMap credentials (`ONEMAP_EMAIL` / `ONEMAP_PASSWORD`) are optional — sign up free at
onemap.gov.sg if you want real Singapore drive times; without them, routing silently falls back
to a straight-line distance estimate.

### Testing

Three tiers, from no-setup to full-stack:

```bash
# 1. Fully offline -- no AWS, no network, no .env needed. Exercises the models, the OR-Tools
#    solver, the intake/reschedule logic, and SQLite persistence with a FakeLLM standing in
#    for Claude.
python -m pytest

# 2. End-to-end with real Claude calls -- needs the AWS credentials + Bedrock access above.
#    Runs the PRD's demo beat: 3 WhatsApp messages -> intake -> sequence -> approve ->
#    reschedule -> re-sequence, printing each step to the terminal.
python scripts/run_demo.py

# 3. Interactive -- same AWS requirement, plus needs jobs already in the DB (run step 2 first,
#    or add jobs another way). Opens the map dashboard in a browser.
streamlit run dispatch_agent/dashboard/app.py
```

If step 2 fails immediately, it's almost always one of: credentials not picked up (check
`.env` was actually loaded — `python -c "from dispatch_agent.config import settings; print(settings.aws_region)"`),
Bedrock model access not yet granted, or a stale `BEDROCK_MODEL_ID`.

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
