# Finalist submission readiness design

## Goal

Make the current Floof demo trustworthy under judging: the tested product, the five-minute flow,
the deck and the README must describe the same system.

## Scope

- Keep the existing LangGraph, Bedrock, deterministic policy and OR-Tools architecture.
- Make `LLM_PROVIDER=none` a real network-free mode and reject unknown providers.
- Restore a green offline test suite without weakening safety assertions.
- Honour client idempotency keys and prevent partial offer acceptance state.
- Redact provider exception details before they reach messages or persisted traces.
- Put the chat setup and next action above the fold.
- After acceptance, lead with the confirmed slot and the new route version.
- Use Floof as the customer-facing product name. Keep Team Majestic Fighters as team attribution.
- Label Friday/Saturday routing, seeded volumes and route timings as demo assumptions.
- Add automated Python, TypeScript and frontend build gates plus two browser demo-path checks.

## Judged path

The difficult path is the primary demo: a customer rejects the first slot, the agent checks the
published Friday and Saturday demo routes, offers three feasible alternatives, the customer picks
the second, and the UI shows the locked promise and the resulting route version. Nothing already
promised may move.

## Sponsor fit

Bedrock reads the customer's wording and selects a legal next action. LangGraph owns the bounded
observe, decide and act loop. Deterministic policy and OR-Tools remain the source of truth. The
demo must expose those boundaries in the trace. AgentCore stays on the roadmap; this PR focuses
judge time on the Bedrock and LangGraph loop already essential to the product.

## Verification

- Full Python suite passes offline without constructing AWS or OpenAI clients.
- Frontend typecheck and production build pass.
- Browser checks cover the happy path and rejection, re-offer, second-choice acceptance.
- Desktop and mobile screenshots show no empty first screen or stale rejection state.
- Search finds one current test count and no unsupported Floof operating claims.
