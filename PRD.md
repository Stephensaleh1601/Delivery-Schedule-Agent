# Dispatch — Product Brief

IGNITE Agentic AI Hackathon 2026, digital track.

## Problem

Fresh food cannot simply be left outside. Staff must first learn when each recipient is available,
then decide whether the delivery route can actually keep that promise. Those two jobs are handled
manually and in different tools.

Informed by an interview with Floof.sg, Dispatch focuses on that coordination gap. The interview
account described roughly 30–40 deliveries in a day and manual customer messaging. Route
optimisation is useful, but the harder product problem is agreeing a feasible time with each
customer.

## Product

Dispatch is an AI delivery coordinator:

1. It reads free-text customer availability.
2. It chooses the bounded workflow allowed for that intent.
3. It checks the relevant published route before proposing a time.
4. It records rejection and searches again when the customer says no.
5. It books only the exact option the customer accepts.
6. It republishes the route without moving existing confirmed promises.
7. It escalates to a coordinator when no safe option exists.

The customer sees a simple chat. The coordinator sees the route impact and an inspectable trace of
every action and tool result.

## Why an agent

A map can sequence known stops. A calendar can store a chosen slot. Dispatch must interpret an
unstructured reply, decide the next action, use several tools, observe the result and act again
when the customer changes the constraints.

The optimiser calculates. The agent owns the loop around it.

## Differentiation

- Offers operationally feasible times, not merely open calendar slots.
- Negotiates under explicit consent instead of silently moving customers.
- Handles rejection as new state and runs another planning cycle.
- Keeps deterministic route and policy truth outside the LLM.
- Makes the full action trail visible to a judge or coordinator.

## Stack

- Python and FastAPI
- LangGraph bounded observe → decide → act loop
- AWS Bedrock for language understanding and action selection
- Pydantic tool contracts and state guards
- OR-Tools for route feasibility
- Google Maps, OneMap or offline routing
- SQLite for customers, messages, offers, traces and route versions
- Next.js operations console
- Playwright browser regression tests

## Demo

### Happy path

A customer gives one available time. Dispatch checks the route, offers a feasible window, receives
acceptance, locks the promise and publishes route v2.

### Difficult path

A customer rejects the first offer. Dispatch excludes it, searches both published routes, returns
three ranked alternatives, accepts the customer's second choice and republishes the day with zero
existing promises moved.

### Three visible proof points

- Natural-language input becomes a constrained tool workflow.
- Rejection visibly changes the next set of options.
- Acceptance visibly changes the route and survives refresh.

## Demo model

The synthetic fixture contains 16 confirmed stops and two waiting customers across two published
routes. Friday/Saturday, regional clusters, service times and the 10 km anchor radius are prototype
assumptions chosen to make the coordination loop repeatable; they are not Floof.sg operating
policy.

## Success evidence

- Happy and difficult paths complete end to end.
- Every offered time contains deterministic route evidence.
- No existing promise moves after a new acceptance.
- A retry cannot create duplicate customer messages or agent runs.
- Offer, order, route and confirmation writes roll back together on failure.
- The full Python, TypeScript, build and Chromium gates pass in CI.

## Deliverables

- Runnable project and README
- Presentation deck, maximum 10 slides
- Functional demo video, maximum 5 minutes
