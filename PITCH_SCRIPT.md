# Dispatch — five-minute pitch script

Spoken narration for the 10-slide deck and `/about` story. Keep the live-demo cues; do not read the
slides word for word.

## 1. The promise

Fresh pet food cannot simply be left outside, so before a driver can follow a route, somebody has
to agree a delivery time with every customer. We built Dispatch: an AI delivery coordinator that
offers only times the route can actually keep. We are Team Majestic Fighters, and this use case was
informed by an interview with Floof.sg.

## 2. The real bottleneck

The interview gave us the scale: roughly 30 to 40 deliveries can run in a day. The difficult part
is not drawing a line between addresses. It is messaging customers, understanding replies like
“Saturday morning” or “not then”, and turning those changing constraints into one workable day.
Every rejection changes the planning input.

## 3. Why existing tools stop short

WhatsApp gets a reply. A calendar stores a booking. Google Maps orders stops it has already been
given. Each is useful, but none decides whether a requested time can safely become a stop, asks for
consent, and then adapts when the answer is no. The optimiser calculates. Dispatch owns the loop
around it.

## 4. One customer journey

The customer speaks normally. Dispatch checks the relevant published route and offers one feasible
window. If the customer accepts, we lock that promise and publish route version two. If they reject
it, we remove that option, search again and return three ranked alternatives. Nobody is moved
without permission, and no route figure comes from the language model.

## 5. What makes it agentic

This is more than a chatbot wrapped around an optimiser. The agent observes the message and current
booking state, selects a legal tool, reads the result, acts, and waits for the next constraint. A
rejection sends it through the loop again. We deliberately separate judgement from truth: the model
chooses the workflow; deterministic policy, consent guards and OR-Tools decide what is feasible.

## 6. Three visible moments

[Cue: open **Customer Chat**.]

First, type “Saturday morning works for me.” Language becomes a route-checked offer. Second, reject
that offer. The agent records the no and produces three different choices. Third, choose option two.
The appointment locks, route v1 becomes v2, and the screen shows that every existing promise stayed
where it was. Those are the three moments to remember: language, adaptation and operational action.

## 7. Architecture

The Next.js console talks to FastAPI. LangGraph runs the bounded observe-decide-act loop. AWS
Bedrock handles free-text understanding and action selection. Pydantic blocks invalid tool calls,
OR-Tools checks route feasibility, and SQLite stores messages, offers, traces and route versions.
Open “Function calls & results” under a reply and you can inspect the exact persisted run that
produced it.

## 8. Sponsor technology is essential

Bedrock and LangGraph are inside the product loop, not added as logos. Without Bedrock, the system
does not understand a customer's changing request. Without LangGraph, it does not observe a result
and act again. The deterministic layer makes those agent decisions safe enough to change an
operation. The existing Python runtime is also ready to move into Bedrock AgentCore as the next
deployment step.

## 9. Evidence, not a mock-up

Our demo has 16 confirmed synthetic deliveries across two published routes and two waiting
customers. The difficult path always returns exactly three route-checked alternatives. Retries do
not create duplicate messages or plans, failed confirmations roll back together, and every judge
path runs in CI. We currently have 496 passing Python tests plus four real Chromium journeys. The
trace on screen is persisted evidence, not an animation.

## 10. The outcome

For the customer, Dispatch feels like a normal conversation and ends with a time they agreed to.
For the coordinator, it removes repeated manual route checks and protects promises already made.
For the driver, it produces one route that reflects the actual customer decisions. Dispatch
negotiates the stops before the driver has to live with them.
