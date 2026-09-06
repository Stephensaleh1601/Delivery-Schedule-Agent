# Delivery coordination policy

The rules the scheduling agent works under. Each has a stable ID so a decision can cite the rule
it followed, and so a rule can be reworded without breaking anything that refers to it.

This file is retrieved by the `retrieve_policy` tool and shown to coordinators and judges. It
explains behaviour; it does not produce it. **Every rule below is also enforced in code**, and the
code is what makes it true — a rule that lived only here would be a suggestion the model could
talk itself out of. Where a rule is enforced, the enforcing site is named.

---

## cluster_days

### CLUSTER-1 — Two delivery days per week

Deliveries run on Friday and Saturday only. No other weekday is a delivery day.

*Enforced by `PlanningClock.coordination_cycle()`.*

### CLUSTER-2 — One region group per day

| Day | Regions |
|---|---|
| Friday | North, North-East, South, East |
| Saturday | Central, City, West |

Every Singapore postal district belongs to exactly one of the seven regions, so every customer has
exactly one **normal** delivery day. This is the first route the agent recommends, not the only
route the customer may ask for. For example, West normally goes on Saturday, but a West customer
may ask for Friday. The agent then tests that customer against Friday's published route and offers
it only if the insertion is feasible. If it does not fit, a human coordinator follows up.

*Enforced by `planning/clusters.py`, over `geo/postal_codes.DISTRICT_TO_REGION`, and by the
requested-day search in `planning/workflows.py`.*

### CLUSTER-3 — Friday and Saturday are coordinated as one weekly pair

A coordination cycle is the earliest Friday and Saturday **of the same week** where both dates are
at least two days away and both already have an active published route.

The two dates are never advanced independently. Doing so could pair one week's Saturday with the
next week's Friday, and the fallback search would then compare two routes the driver never runs in
the same week. If no complete pair exists, the agent reports `no_coordination_cycle` and the
customer goes to a human coordinator.

*Enforced by `PlanningClock.coordination_cycle()`.*

### CLUSTER-4 — Never create a delivery day

The agent may only insert customers into routes that already exist and are published. It never
creates a delivery day, and never opens an empty route.

*Enforced by `get_existing_routes` and the insertion service, which read active plan versions
only.*

### CLUSTER-5 — Saturday is normal for West, not the only day they may request

The region map chooses the route to recommend first. It is not a ban on the other delivery day.
A West customer is normally offered Saturday, but if Saturday does not suit them, the agent may
test that customer against Friday's existing published route. The reverse also applies to a
Friday-region customer asking for Saturday. The requested day is offered only when the insertion
is feasible; otherwise a human coordinator follows up.

*Enforced by the requested-day search in `planning/workflows.py`.*

---

## delivery_windows

### WINDOW-1 — Three broad arrival windows

| Window | Promised arrival |
|---|---|
| Morning | 10:00 – 14:00 |
| Afternoon | 14:00 – 17:00 |
| Evening | 17:00 – 21:00 |

### WINDOW-2 — A window is a promise of arrival, not an appointment

The driver arrives inside the window. The customer is never given a narrower time, because the
business does not commit to one.

*Enforced by `planning/promise_window.py`, which sets the promise to the window containing the
solved arrival.*

### WINDOW-3 — Service and the journey home may run past the window

The arrival must fall inside the promised window. The delivery itself, and the drive back to the
depot, may finish after it. A route must be back at the depot by the hard route end.

| Boundary | Time | Meaning |
|---|---|---|
| Arrival cutoff | 21:00 | No customer is promised an arrival after this |
| Soft day end | 22:00 | Work past this is counted as overtime and penalised |
| Hard route end | 22:30 | A route that returns later is infeasible |

An ordinary evening delivery is not overtime. The soft end sits past a normal completion —
arrival, service, and the drive home — precisely so that the scorer does not learn to avoid the
evening window.

*Enforced by `solver.py` (arrival clamp and depot window) and `planning/scoring.py` (overtime).*

---

## alternatives

### ALT-1 — The normal offer is one proven option

A customer is offered a single window on their cluster day. Belonging to a region is not evidence
that the day can take them: the insertion must be calculated and proven feasible first.

### ALT-2 — The fallback offer is exactly three

If the normal option is rejected, or no insertion fits the cluster day at all, both the Friday and
Saturday routes are searched and exactly three choices are offered. If fewer than three valid
choices exist, the customer goes to a human coordinator.

*Enforced by the offer purpose caps in `planning/offer_service.py`.*

### ALT-3 — An anchor must be within 10 km

A customer may only be inserted next to an existing stop within 10 km of them. Stops further away
are not considered.

### ALT-4 — Existing stop order is preserved

Inserting a customer never reorders the route. Each candidate position is tested immediately
before or immediately after a nearby anchor stop, and everything else stays where it was.

### ALT-5 — No insertion may make an existing customer late

A candidate is rejected if it would push any later stop outside the window that customer was
promised, or return the route to the depot after the hard route end.

Nobody is moved by an insertion, but everybody after it can be delayed by one — so this is checked
explicitly rather than assumed.

### ALT-6 — Ranking is deterministic

Surviving choices are ranked by added distance, then added driving minutes, then earlier date,
then earlier window, then earlier insertion position. The same data always produces the same
three.

The added distance of an insertion between stops P and N is:

```
d(P, customer) + d(customer, N) − d(P, N)
```

At the start or end of a route, P or N is the depot.

### ALT-7 — Preference orders the normal offer, not the fallback

A customer's stated preference decides what is tried first for the normal offer. It does not
reorder the fallback ranking: there, excluded choices are removed and the rest are ranked by
ALT-6 alone.

### ALT-8 — A ruled-out time is never offered

A preference ("Friday morning works for me") is a starting point. A restriction ("only Friday
morning", "I cannot do Saturday") is binding, and the agent never offers a day or window the
customer has excluded — including when that leaves fewer than three choices, which escalates
instead.

*Enforced by the exclusion filter in the insertion service.*

### ALT-9 — Nothing is confirmed without an explicit acceptance

An offered choice becomes a booking only when the customer accepts that specific choice.

*Enforced by `lock_appointment`, which refuses unless the acceptance names the offer and slot.*

### ALT-10 — An offer may go stale

A route can change between offering a slot and the customer accepting it. On acceptance the stored
route version is checked; if the route has moved, the insertion is recalculated and confirmed only
if the same promise still holds. Otherwise the customer is told, and the choice is recalculated or
escalated.

---

## driver_dispatch

### DRIVER-1 — One driver per route

Each published route has one assigned driver.

### DRIVER-2 — The route is sent by a coordinator, not by the agent

The finished route is sent to the driver from the coordinator's console, the day before delivery.
A customer conversation can never send a route: the dispatch action is not one of the agent's
tools.

*Enforced structurally — `send_driver_route` is not registered in the tool registry.*

### DRIVER-3 — What the driver receives

The stops in order, each with its address and broad window, and a Google Maps link covering the
whole route. The route version and send time are recorded.

---

## attendance

### ATTEND-1 — Somebody must be home

The customer, or someone acting for them, has to be present to take the delivery in person. This
is why a time is agreed with the customer at all rather than simply announced at them: the whole
booking conversation exists to find a window they can actually be in.

### ATTEND-2 — Food is never left unattended

The driver does not leave an order at the door, with a neighbour, in a lobby, at a concierge or in
a parcel locker, and does not leave it with building security. There is no "leave it outside"
option, and the agent cannot arrange one.

### ATTEND-3 — Why: it is fresh food

The orders are freshly made pet food, not ambient goods. Left in Singapore's heat it spoils within
hours and is no longer safe to feed an animal. The attendance rule is a food-safety rule, not a
convenience preference.

### ATTEND-4 — Nobody home

If nobody is there when the driver arrives, the food cannot be left and the delivery is not
completed. The order goes back to a coordinator, who arranges a new day with the customer. A
missed delivery is rebooked by a person, not automatically by the agent.

---

## general_enquiries

### GEN-1 — Answer only from this policy

Customer questions about how delivery works are answered from the rules in this file and from
nothing else. Where this file does not cover a question, the honest answer is that it cannot be
confirmed here, followed by an offer to have a coordinator follow up.

*Enforced by `search_delivery_policy`, which returns rule text or nothing.*

### GEN-2 — A question changes nothing

Answering a question never records availability, creates or confirms an offer, records a
rejection, alters a route or dispatches a driver. A customer who asks what the windows are has not
booked anything.

*Enforced by `INTENT_TOOLS["policy_question"]`, which exposes only the search, the reply, the
escalation and finish.*
