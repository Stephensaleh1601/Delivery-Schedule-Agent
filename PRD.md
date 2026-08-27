# Dispatch Sequencing Agent — PRD

IGNITE Agentic AI Hackathon 2026, digital track.

---

## The problem

A scheduling coordinator at a Singapore home-installation company needs a way to offer each
customer a time slot that actually fits the day's route, because every slot she promises is
chosen from memory, and one reschedule forces her to rebuild the whole day by hand.

That's the statement. It names a person and a moment, it carries no technology, and it stays
true whatever anyone builds.

## Why it's a real problem

For jobs like furniture delivery, appliance installation and home internet setup, the customer
has to be there. So you can't just cluster by region and send a driver. The day is shaped by
when people are free.

The coordinator picks slots from memory. Nobody checks afterwards whether the order she picked
was any good, because no one records how long the jobs actually took. When one customer moves,
every job after it has to be reshuffled by hand.

I interviewed a Singapore logistics operator in July and saw the same pattern there: the day's
sequence is worked out on paper, the route is chosen on who knows the area, and nothing is
measured. I can talk the team through what I learned, but it stays verbal.

**Still to do:** one recorded conversation with someone who actually runs installation bookings.
Right now the statement above is built on a pattern I've seen next door, not on the exact user.

## What we're building

Two agents and a human approval step.

**Intake agent.** Reads the WhatsApp message, pulls out name, address, postal code, job type and
when the customer is free. Outputs a structured job record.

**Planning agent.** Takes the day's jobs, gets real drive times between them, and works out an
order that fits everyone's availability. Outputs a sequence with a proposed arrival window per
customer.

**Human approval.** The coordinator sees the proposed day on a map and approves, edits or rejects.
Nothing goes to a customer without her.

**Reschedule loop.** Customer says "Thursday instead". The plan re-solves, works out which other
customers are now affected, and proposes new slots for just those. Back to approval.

Every edit she makes gets logged.

## What we're not building

Quotation, job sheet, contractor handoff, delivery notification, invoicing, payment. That's the
rest of the workflow and it's out. We stop once the timing is confirmed.

## Why it needs an agent

A calendar takes a time you already picked. This picks the time, based on where the day's other
jobs are, and re-derives the whole thing when one customer moves.

That's the three things the brief asks for. It plans the day, it acts by calling routing and
drafting messages, and it adapts when the day changes.

## What makes it different

A routing API gives you the optimal sequence. It can't tell you whether the human who ignored it
was right. We propose, she corrects, and we keep the record of where the two disagreed.

Nobody's mapping product does that because it needs memory of one specific operation.

It also answers the obvious objection, that experienced staff won't use an optimiser. She's never
told she's wrong. She approves a draft.

## Stack

Python. LangGraph for the two agents. Claude Haiku 4.5 on Bedrock. Pydantic for the job schema.
Google OR-tools for the sequencing, which handles time windows and is exact at our size. A maps
API for real drive times. SQLite for jobs and the override log. Deployed on AgentCore. Small
dashboard showing the day on a map.

Reusing three files from Stow: the Singapore postal-code-to-coordinates map, the zone centroids,
and the routing client. That last one has a filter that rejects routes cutting through Johor,
which the maps API does sometimes for local trips. It will happen during a demo otherwise.

## How we show it works

Three numbers on a slide:

- How often the intake agent produces a usable job record first try
- How often a message goes to an approved slot with no human edit
- Total drive time of the agent's day against the same day sequenced by hand

The third one is the headline. We need a real person to sequence the same ten jobs on paper so
the comparison isn't us against ourselves.

## The demo

Five minutes. The middle two are the only part that matters.

Three messages arrive. The agent extracts them, proposes a sequenced day on the map, coordinator
approves. Then customer two asks to move to Thursday. The day re-solves, two other customers are
affected, new slots go up, coordinator approves again.

Build that reschedule beat first. Everything else is setup.

## Open

**Which user.** This is written around the installation coordinator, because the customer has to
be home and that's what makes the reschedule case interesting. The other option is the dispatcher
at a logistics company, where I have better evidence but there's no customer conversation, which
deletes the intake agent. Team call.

**How much of "adapts" we claim.** The reschedule replan is real and we can demo it. Learning from
the override log is a schema and a roadmap slide. We should say the second one is roadmap rather
than imply it's working.

## Deliverables

- Project files, max 5GB, one submission only
- README with how to run it and what each file does
- requirements.txt and .env.example
- Deck, max 10 slides
- Video, max 5 minutes
