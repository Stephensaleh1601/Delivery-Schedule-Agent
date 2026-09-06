# Dispatch — five-minute pitch script

Spoken narration for the seven-slide deck at `/about`, including the planned
cutaways to the working product.

## 1. The ask — presentation

> Nobody should spend an afternoon asking forty people when they are home.
>
> We spoke to Floof.sg, a Singapore company that delivers fresh pet food. Every
> delivery needs an agreed recipient window, so somebody has to coordinate
> customers and the route at the same time.
>
> We are Team Majestic Fighters, and we built Dispatch to do that work.

## 2. The problem — benefits and presentation

> Floof has two hard jobs. First, a coordinator asks customers when they can
> receive their order. Second, those answers have to become a sensible driving
> sequence.
>
> Floof reported that a busy day can reach roughly thirty to forty deliveries.
> The hardest part was not drawing a route. It was negotiating delivery slots
> with customers. One rejection changes both the conversation and the plan.

## 3. What we built — effectiveness

> [Cut to Customer Chat.]
>
> The customer writes in their own words. The agent checks the published route
> and offers one time it can actually keep.
>
> Mrs Chua accepts the first offer. Mr Rajan rejects his, so the agent records
> the rejection, excludes that slot and searches again. If policy cannot produce
> a complete fallback set, it hands the case to a person instead of guessing.
>
> Nothing moves until the customer agrees.

## 4. How it is built — innovation and technical quality

> This is the loop. The agent asks, “What should I do next?”, chooses an approved
> action, reads the result and decides again.
>
> Policy tools find the relevant rule. Route tools calculate feasible windows.
> Action tools explain, confirm or escalate.
>
> AWS Bedrock handles language and action selection. Deterministic code owns
> dates, distance, consent and route truth. LangGraph keeps the loop bounded and
> records each step.

## 5. Fitting people in — innovation and technical quality

> [Cut to Daily Routes.]
>
> Our repeatable demo has two published delivery days. A postal region tells the
> agent which route to check first.
>
> It tests the new customer around nearby stops, then checks every later promise.
> Existing confirmed stops keep their order. Nobody already booked is made late.
> A rejection opens the bounded fallback search, not an unlimited negotiation.

## 6. The prototype — effectiveness and technical quality

> The product has three working views: orders, daily routes and customer chat.
> The customer gets one simple reply. The coordinator can open the trace and see
> what the agent read, which route it checked and why it made that offer.
>
> The working stack is Python, FastAPI, Next.js, LangGraph, AWS Bedrock, OR-Tools
> and SQLite. The customer channel and address lookup are clear integration
> points for the operator’s systems.

## 7. What changes — benefits

> With Dispatch, a rejection no longer sends a coordinator back to the start.
> The agent excludes it, replans and asks for consent again.
>
> Every customer gets a time they agreed to. Every driver gets a route that
> makes sense. The coordinator handles the exceptions instead of spending the
> day on WhatsApp.
