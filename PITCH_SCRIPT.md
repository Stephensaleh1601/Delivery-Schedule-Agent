# Pitch script — The Dispatch Agent

Spoken narration for the 7-slide deck at `/about`, one section per slide. Roughly 30–45 seconds
a slide — about 5 minutes end to end, plus time for the live demo cutaways marked below.
Source of truth for the *content* is `slides.md`; this is only the delivery, so update both if
the slides change.

---

## 1. Title — *presentation*

> Nobody should spend an afternoon asking forty people when they're home.
>
> That's the line we kept coming back to, based on our interview with the Founder of Floof.sg, a Pet Food Company in Singapore which delivers fresh pet food. This means every
> order needs someone standing at the door to receive it. 
> We're Team Majestic Fighters, and we have built the AI Dipatch Agent to help companies like floof handle delivery route scheduling.

---

## 2. The problem — *presentation*

> Floof.sg has two hard jobs, and right now both are done manually.
>
> Firstly, they get at least forty orders a week, one person has to spend a whole afternoon on WhatsApp messaging customers on delivery timings 
> if one customer says no, you're starting that conversation over. 
> Here's the founder, in their own words about the difficulty of neogtiating timings with customers
> Secondly: the person still has to decide the sequence of delivery. Getting that wrong means long detours, and the last customer on the list waits all day.
> So the real problem is coordinating a time with every customer, and put them in a sensible driving order — at the same time.

---

## 3. What we built — *effectiveness*

> [Cue: switch to the **Customer Chat** tab]
>
> The customer texts in their own words. The agent reads it, checks the real route for that day, and offers back one time it can actually keep.
> Watch what happens with two different customers. Mrs Chua says yes straight away. 
> Mr Rajan says no, twice — so the agent looks wider, comes back with three real alternatives, and if none of those work either, it hands him to a person rather than guessing.

---

## 4. How it is built — *technical quality*

> Here's the architecture, 
> A message comes in, gets turned from the customer's own words into real dates using `understanding.py` and `language.py`. 
> Then the main agent picks the relevant tools which are categorised into the Policy, Route and Action tools to determine what to do
> this way it replicates how a real person would think and reduce the mistakes made

---

## 5. Fitting someone into a day that's already planned — *technical quality*

> [Cue: switch to the **Daily Routes** tab]
>
> In this use case, Floof delivers two days a week, and each day covers one half of Singapore
>so a customer's address already tells us which day is theirs. From there we can find a gap in a route and determine whether it fits 


---

## 6. What the office sees — *technical quality*

> Our current prototype is able to track every order, view the daily routes and observe customer conversations 


---

## 7. What changes, and what's next — *benefits*

> So — overall, the agent is about reply customers, think of the best routes and schedule them according to customer's request and proximity

>Some improvements we can make is to add more days, more drivers and customisable to other industries

> Therefore, with our dispatch agent, every customer gets a time they agreed to, every driver gets a route that makes sense, no more afternoons spent glued to WhatsApp
