# Slide 1 — Title

*Judging criterion: presentation*

## Nobody should spend an afternoon asking 40 people when they are home

An AI helper that agrees a delivery time with every customer by text message —
and only offers times the van can actually make.

**Built with** Floof.sg, a Singapore company that delivers fresh pet food
**IGNITE Agentic AI Hackathon 2026** · Digital track
**Team:** Majestic Fighters

---

# Slide 2 — The problem

*Judging criterion: presentation*

## Two hard jobs, both done by hand

Floof.sg makes fresh pet food. It goes bad in Singapore's heat, so it can't be
left at the door. **Someone has to be home.** That means every order needs a
time the customer said yes to.

### Hard job 1 — asking everyone

```
   40 orders                one person,               and if one person
   for the week      →      a whole afternoon   →     says "not Friday",
                            of WhatsApp               you start again
```

> "It's more to negotiate with the customers when is the delivery time slots
> they prefer. **That's the hardest part.**"
> — Floof.sg, 5 Sep 2026

### Hard job 2 — deciding who to visit first

```
   Good order:   depot → A → B → C → D → home      short drive, everyone on time

   Bad order:    depot → A → D → B → C → home      long detours, and the
                          └──────┘  └──┘           last people wait all day
```

Getting the order wrong means big detours. Big detours mean late deliveries.
Right now a person works this out by hand — and drivers still change it on the
road when something comes up.

### The problem, in one sentence

> A delivery coordinator at Floof.sg needs a way to agree a time with every
> customer **and** put them in a sensible driving order, **because** doing both
> by hand takes a whole afternoon, and one "sorry, not Friday" undoes it.

---

# Slide 3 — What we built

*Judging criterion: effectiveness*

## The customer texts. The helper answers with a time it can keep.

```
   Customer                    Our helper                 The van's day
   ────────                    ──────────                 ─────────────

   "I'm free                 reads the                  looks at Friday's
    Saturday      ──────►    message      ──────►       real route
    morning"                                                  │
                                                              ▼
   "Yes, that                 offers ONE                 finds a gap
    works"        ◄──────     time it can  ◄──────       between two
                              keep                       real stops
        │
        ▼
   Booked. The route updates. Nobody else gets moved.
```

No forms. No "please give us three options". One message, one answer.

### The two customers in our demo

| | what they do | what the helper does |
|---|---|---|
| **Mrs Chua** | says yes straight away | one search, one offer, done |
| **Mr Rajan** | says no, twice | looks wider, offers three, or asks a person for help |

About 7 in 10 customers are like Mrs Chua. We built it so the other 3 still get
a good answer.

---

# Slide 4 — Why an AI agent, and not just a form

*Judging criterion: originality*

## A form can take a time. It can't have a conversation.

```
   A FORM                              OUR AGENT

   asks a fixed question               reads what they actually wrote
   takes a fixed answer                "Sat morning", "after 1", "not Friday"

   offers whatever is free             offers what the van can really do
                                       and explains why

   gives up when they say no           tries somewhere else when they say no

   answers nothing else                answers "why that time?" and
                                       "can you leave it at my door?"
```

An "agent" just means software that can **plan**, **do things**, and **change
its mind** when it gets new information:

- **Plans** — works out where this customer fits into a day that's already busy
- **Does** — offers a time, books it, replies, updates the route
- **Changes its mind** — a "no" makes it search somewhere different, not just
  say sorry

---

# Slide 5 — How it is built

*Judging criterion: technical quality*

## Everything comes back to the same question

Each box is marked **[AI]** or **[CODE]**. The AI appears twice — and neither
time does it touch a number.

```
        A customer texts  —  or a scheduled run starts
                     handle_planning_event()
                              │
                              ▼
                Work out what they want        [AI] [CODE]
          Their words, quoted. Then turned into real dates.
        agents/understanding.py → planning/language.py
                              │
                              ▼
                         MAIN AGENT            [AI]
                   "What should I do next?"
          agents/scheduling_agent.py  observe → decide → act
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  POLICY TOOLS           ROUTE TOOLS           ACTION TOOLS
  reads the KB:          reads the published   reads the order book
  delivery-policy.md     routes, measured      and the customer's
  26 numbered rules      with OR-Tools         thread

  search_delivery_       find_normal_slot      confirm_offer
    policy                 their own day         book what they took
    find the rule        find_requested_       explain_offer
  retrieve_policy          day_slot              say why that time
    pull up a topic        the day they named  escalate_booking
                         find_fallback_          hand to a person
                           options             send_message
                           both days, only       send the wording
                           after a "no"          the tool produced
        │                     │                     │
    comes back            comes back            comes back
        │                     │                     │
  The rule text,        Times PROVED against  A booking, and one
  and its number        the real route,       reply the customer
  (WINDOW-1)            never guessed         actually sees
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              ▼
                MAIN AGENT DECIDES AGAIN       [AI]
            Given what came back — is this finished?
                              │
                              ▼
                  Finish, or go round again
                     MAX_TOOL_STEPS = 10
          a counter in the code, not the AI's judgement
```

The finished route goes to the driver from the coordinator's screen, the day
before. That is deliberately **not** one of the agent's tools — a customer
conversation can never dispatch a van.

**Six actions, not sixty.** Each is a whole job, the way a person would think
about it. A long list of small steps is a long list of chances to pick wrong.

**It can't search the wrong day.** Which days an action may read is built into
that action, not typed in by the AI. "Their usual day" can only see one day.

---

# Slide 6 — The one big rule

*Judging criterion: originality*

## The AI chooses. The maths decides what's true.

```
   ┌──────────────────────────┐      ┌──────────────────────────┐
   │         THE AI           │      │        THE MATHS         │
   │                          │      │                          │
   │  reads the message       │      │  works out the dates     │
   │  picks one of 6 jobs     │      │  measures the driving    │
   │  repeats their words:    │      │  checks the time fits    │
   │   "Saturday morning"     │      │  puts options in order   │
   │                          │      │                          │
   │  never works out a date  │      │  never guesses           │
   │  never invents a time    │      │                          │
   └──────────────────────────┘      └──────────────────────────┘
             chooses                          decides
```

AI can sound confident and still be wrong. "Next Tuesday is the 15th" — is it?

So we never let it do the sums. It only repeats the customer's own words
("Saturday morning"), and our code works out what that actually means. **There
is nowhere for a made-up date to get in.**

### We tested this on a real AI. Twice it misbehaved:

| What it did | What we did about it |
|---|---|
| Booked a slot while its own question was still unanswered | Now the code refuses unless the customer really said yes |
| Rewrote our message into "Dear Mrs Lee… Best regards" and deleted the reason | Now the code sends the message, not the AI's rewrite |

Telling it "please don't" didn't work. So we made it impossible.

---

# Slide 7 — Fitting someone into a day that's already planned

*Judging criterion: technical quality*

## Slide them in. Don't rebuild the whole day.

Floof.sg delivers on **two days a week**. Each day covers one part of Singapore:

```
   FRIDAY                              SATURDAY
   North · North-East · South · East   Central · City · West
```

So your address already tells us your normal day. Then we look for a gap:

```
   Friday's van, already planned:

   depot ──► Chen Li Hua ──► [ NEW HERE? ] ──► Marcus Tan ──► ... ──► depot
                    ▲               ▲                ▲
              is the new       we try both      does everyone
              customer         before and       after still
              nearby?          after            arrive on time?
              (within 10km)
```

| The rule | In plain words |
|---|---|
| Don't shuffle | everyone already booked stays exactly where they are |
| Don't make anyone late | we check every later stop, we don't just hope |
| Don't add a new day | it can only fill days the van is already driving |
| One offer, or three | one good time normally; three only if they say no |

Customers get a wide window — **morning 10–2, afternoon 2–5, evening 5–9** —
because that's what the business can honestly promise. Not a fake 15-minute slot.

---

# Slide 8 — What the office sees

*Judging criterion: technical quality*

## Three screens, and the helper shows its working

```
   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
   │   ORDERS     │  │    ROUTES    │  │     CHAT     │
   │              │  │              │  │              │
   │ every order  │  │ Friday and   │  │ the customer │
   │ and where    │  │ Saturday on  │  │ chat, and    │
   │ it's up to   │  │ a map, in    │  │ what the     │
   │              │  │ visit order  │  │ helper did   │
   └──────────────┘  └──────────────┘  └──────────────┘
```

While the customer waits a few seconds, the screen says what's really happening
instead of just spinning:

```
   ✓ Read the message                        0.9s
   ✓ Checked Friday's route — 17 stops       2.3s
   ✓ Offered 2–5pm, right after Chen Li Hua  0.4s
```

Every line is written by the part of the program that actually did that thing,
at the moment it did it. Nothing is faked to look busy.

### What production still needs

| Ready now | Swap-ins, clearly marked |
|---|---|
| The agent, the route maths, the written rules, the console, the database | Real WhatsApp instead of our simulated thread |
| Python · FastAPI · Next.js · OR-Tools · Claude on AWS Bedrock | Floof.sg's own address lookup |

Each swap-in sits behind one interface, with a working stand-in today. Nothing
else has to change around it.

---

# Slide 9 — How do we know it works?

*Judging criterion: effectiveness*

## We check it, we don't just claim it

| What we check | Where it stands |
|---|---|
| **Automatic tests** | 440 passing — they run every rule on slide 7 |
| **Tests that call the internet** | none, on purpose. So results never change by accident |
| **Can it get stuck in a loop?** | no — it's cut off after 10 steps, and it can't argue |
| **Can it invent an action?** | no — anything it makes up is refused, not guessed at |
| **Can we see why it said that?** | yes — every answer points to the written rule behind it |

### Two things we chose *not* to do

**We never show one big "score".** A single number that mixes driving minutes
with made-up penalties looks meaningful and isn't. We show the real parts
instead: minutes, kilometres, extra hours.

**We never let the AI describe the route.** It would happily say "we'll be in
the East that morning" when the van isn't going there. Those sentences are
built from the real planned route.

---

# Slide 10 — What changes, and what's next

*Judging criterion: benefits*

## An afternoon of texting becomes something that answers itself

| Before | After |
|---|---|
| One person, one afternoon, 40 customers | Each customer answered as they reply |
| Drivers sorting out times between stops | Drivers get a finished route the day before |
| "Not Friday" means starting over | A "no" makes it look somewhere else, by itself |
| The plan is in one person's head | Every decision can be looked up later |

### Why it spreads easily

| | |
|---|---|
| **More days** | Delivery days are settings, not code. Add Tuesday and it plans Tuesday |
| **More vans** | The route maths already solves one day. A second van is a second day to solve |
| **Other trades** | Anything where somebody must be home: groceries, medicine, repairs, installs |

```
   now              →   regular customers   →   more days,        →   any delivery
                        get rebooked            more vans             where someone
   two days,            automatically                                 must be home
   one van each         every week
```

This isn't really about pet food.

> Every customer gets a time they agreed to.
> Every driver gets a route that makes sense.
> Nobody spends an afternoon on WhatsApp.
