# Slide 1 — Title

## Smarter delivery promises for purchases that cannot be left at the door

An AI scheduling agent that finds a customer-approved delivery time while minimising delivery cost.

**[Product name]**  
SimplifyNext Agentic AI Hackathon 2026  
**Team:** [Names]

---

# Slide 2 — The problem

## One experienced coordinator is holding the entire process together

Large purchases such as doors, furniture and appliances cannot simply be left outside. The customer must be present to provide access and accept the delivery.

From our conversations with delivery businesses, the current process is highly manual:

```text
Contact every customer
        ↓
Negotiate several possible timings
        ↓
Record everything in a spreadsheet
        ↓
Manually group and sequence deliveries
        ↓
Redo the plan whenever something changes
```

> “One person sits down, collects a few possible timings from every customer, then maps the routes using their experience of Singapore’s roads.”

*Paraphrased from industry conversations.*

### Why this matters

- Route quality depends on one employee’s personal knowledge.
- Customer availability and route efficiency are planned separately.
- A cancellation or product delay can force the route to be rebuilt.
- A failed delivery is costly because the item cannot be left unattended.

---

# Slide 3 — The planning goal

## Find a customer-approved timing with the lowest delivery cost

The objective is not simply to find the shortest route or automatically accept the customer’s first preference.

### Customer suitability comes first

The agent removes any timing that:

- Is outside the customer’s stated availability
- Has insufficient driver or vehicle capacity
- Would exceed working hours
- Has a product that is not ready
- Would break another confirmed appointment

### Route efficiency decides between the workable options

For every feasible timing, the agent measures:

- Additional driving time and distance
- Route completion time
- Potential driver overtime
- Whether another delivery day must be opened
- Available vehicle and driver capacity
- Customer preference

```text
Customer-approved timings
            +
Operational route impact
            ↓
Best overall appointment options
```

### Why plan 2–4 days ahead?

Based on our industry conversations, coordinators typically plan several days in advance.

This gives the agent enough flexibility to group nearby deliveries while remaining close enough to know which products will actually be ready.

### What “cost” means

For the prototype, cost is represented as **route impact**:

```text
Route impact =
additional driving
+ overtime impact
+ opening an otherwise empty delivery day
```

It is not yet a dollar amount.

In production, it can be converted into money using:

```text
Driver and vehicle hours
+ fuel or charging cost per kilometre
+ overtime
+ additional vehicle deployment
```

> The agent protects the customer’s acceptable timings while minimising the operational cost of fulfilling that promise.

---

# Slide 4 — Technical architecture

## One scheduling agent connects the customer, delivery data and route engine

### Architecture diagram

```text
                         PLANNING TRIGGERS
            Product ready • Customer reply • Product delay
                                  │
                                  ▼
┌──────────────┐       ┌───────────────────────────┐
│   Customer   │◄─────►│ Mock WhatsApp Conversation│
└──────────────┘       └─────────────┬─────────────┘
                                     │
                                     ▼
                          ┌────────────────────────┐
                          │    Scheduling Agent    │
                          │ Claude + AWS Bedrock   │
                          │ LangGraph workflow     │
                          └────────────┬───────────┘
                                       │
       ┌───────────────────────────────┼────────────────────────────┐
       ▼                               ▼                            ▼
┌──────────────────┐       ┌────────────────────┐       ┌────────────────────┐
│ Customer Tools   │       │ Route Evaluation   │       │ Operations Tools   │
│ Create offer     │       │ OR-Tools solver    │       │ Lock appointment   │
│ Send message     │       │ Google Maps data   │       │ Replan route       │
└────────┬─────────┘       └─────────┬──────────┘       └─────────┬──────────┘
         │                           │                            │
         └───────────────────────────┼────────────────────────────┘
                                     ▼
                          ┌────────────────────────┐
                          │ Orders and Route State │
                          │ FastAPI + SQLite       │
                          └────────────┬───────────┘
                                       │
                         ┌─────────────┴─────────────┐
                         ▼                           ▼
              Confirmed customer slot      Daily driver route
```

### Core entities

```text
Customer
   ↓
Order
   ↓
Availability windows
   ↓
Appointment offer
   ↓
Confirmed appointment
   ↓
Route plan and numbered stops
```

The system also stores each route version, agent run and actual tool call for traceability.

---

# Slide 5 — Expected user flow

## The customer sees a simple conversation; the agent performs the planning behind it

| Customer experience | What happens behind the scenes |
|---|---|
| Product becomes ready for delivery | A product-readiness event starts the scheduling workflow |
| Customer receives a WhatsApp message | The agent requests 2–3 available windows, 2–4 days ahead |
| Customer submits several possible timings | Availability is saved against the order |
| Customer sees “Checking available delivery slots…” | `evaluate_slots` tests every timing using OR-Tools and Google Maps |
| Customer receives the best workable options | `create_offer` selects the feasible options with the lowest route impact |
| Appointment options arrive through WhatsApp | `send_message` delivers the offer |
| Customer confirms an option | `lock_appointment` protects the customer promise |
| Customer receives confirmation | The affected route is published as a new version |
| Customer receives a delivery reminder | The coordinator and driver receive the final numbered route |

### Actual agent tool sequence

```text
evaluate_slots
Tests every customer-approved timing against its possible route
        ↓
create_offer
Chooses the best feasible appointment options
        ↓
send_message
Sends those options through WhatsApp
        ↓
lock_appointment
Locks the selected appointment and updates the route
        ↓
finish
Records that the task has completed
```

### When something changes

```text
Product becomes delayed
        ↓
replan_day
Rebuilds the route without moving confirmed appointments
        ↓
find_ready_replacements
Finds another waiting customer who can use the freed capacity
        ↓
create_offer
Offers the recovered slot through WhatsApp
        ↓
Customer accepts
        ↓
Appointment locks and the next route version is published
```

The interface shows the real tool name, input, result and a short decision summary. It does not expose private chain-of-thought.

---

# Slide 6 — Impact and conclusion

## Better customer choice without sacrificing delivery efficiency

### New appointment

```text
Customer provided             3 workable timings
Options evaluated             3 upcoming delivery routes
Selected appointment          [Date and time]
Route version                 v1 → v2
Additional driving            +[X] minutes
New delivery day required     No
Confirmed appointments moved  0
```

### Operational disruption

```text
Product delayed               1 order removed
Route version                 v2 → v3
Driving time                  [X] min → [X] min
Freed capacity                1 delivery slot
Replacement delivery offered  1
Confirmed appointments moved  0
```

*Replace the bracketed values with the final clean demo results.*

### Business value

- Less coordinator time spent negotiating and rebuilding routes
- Lower driving, overtime and vehicle-deployment costs
- Fewer failed delivery attempts for high-value purchases
- Route-planning knowledge is no longer trapped with one employee
- Confirmed customer promises remain protected during disruptions
- Applicable to doors, furniture, appliances, installations and field services

> Every customer gets a workable choice. Every driver gets a better route. Every confirmed promise remains protected.

**Implementation note:** the application currently allows N+2 to N+5. Change the maximum to N+4 if the presentation and interview evidence will consistently say “2–4 days ahead.”