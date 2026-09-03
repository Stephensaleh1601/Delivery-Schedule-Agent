"""A deliberate live-provider smoke test. NOT part of the test suite.

The automated tests never call a model -- an autouse fixture pins LLM_PROVIDER=none, because a
suite that quietly bills an account is a suite people stop running. But "the demo uses a real
model" is a claim, and a claim nobody has checked is a rehearsal risk, so this script exists to
check it on purpose, by hand, before recording.

    .venv/Scripts/python.exe scripts/smoke_live_model.py

It reads credentials from .env.local exactly as the app does, sends a handful of real messages
through the real reader, and prints what came back next to what the deterministic parser would have
said. Nothing is written to the database.

What it proves, and what it does not: it proves the provider is reachable and returns a usable
tool call. It does not prove the model is good at this -- the phrases it quotes are re-parsed here
anyway, which is the point of the architecture.
"""
from __future__ import annotations

import sys

from dispatch_agent import config
from dispatch_agent.agents.understanding import MessageReader
from dispatch_agent.planning import language

# The messages the demo actually uses, plus the two that are easiest to get wrong.
CASES = [
    ("I'm free Saturday morning.", False),
    ("Any time after 1 on Tuesday", False),
    ("I can do Saturday afternoon or Tuesday morning, but Tuesday is better.", False),
    ("Saturday morning, that's the only time I can do.", False),
    ("That time doesn't work. Can you do later?", True),
    ("Why this timing?", True),
    ("Okay, take the first one.", True),
    ("lol", True),
]


def describe(interpretation) -> str:
    windows = "; ".join(
        f"{w.date} {w.window.start:%H:%M}-{w.window.end:%H:%M}" for w in interpretation.windows
    )
    flags = []
    if interpretation.is_fixed:
        flags.append("fixed")
    if interpretation.rejects_whole_day:
        flags.append("whole-day")
    if interpretation.accepted_ordinal:
        flags.append(f"#{interpretation.accepted_ordinal}")
    return f"{interpretation.intent:22} {' '.join(flags):12} {windows}"


def main() -> int:
    provider = config.settings.llm_provider
    if provider in ("none", ""):
        print("LLM_PROVIDER is 'none' -- nothing to smoke test. Set it in .env.local first.")
        return 1

    model = (
        config.settings.bedrock_model_id
        if provider == "bedrock"
        else config.settings.openai_model
    )
    print(f"provider : {provider}")
    print(f"model    : {model}")
    if provider == "bedrock":
        print(f"region   : {config.settings.aws_region}")
    print(f"today    : {language.today()} (Singapore)")
    print("=" * 100)

    reader = MessageReader()
    failures = 0

    for message, has_offer in CASES:
        understood = reader.read(message, has_open_offer=has_offer)
        rules = language.interpret(message, has_open_offer=has_offer)

        agreed = understood.interpretation.intent == rules.intent
        mark = "ok " if understood.used_model else "FELL BACK"
        if not understood.used_model:
            failures += 1

        print(f"\n{message!r}   (offer on the table: {has_offer})")
        print(f"  {mark:10} {describe(understood.interpretation)}")
        print(f"  {'rules':10} {describe(rules)}{'' if agreed else '   <- differs'}")
        if understood.fallback_reason:
            print(f"  reason     {understood.fallback_reason}")

    print("\n" + "=" * 100)
    if failures:
        print(
            f"{failures}/{len(CASES)} messages fell back to the deterministic reader. The demo will "
            f"still work, but the run log will say 'model unavailable' -- fix the credentials "
            f"before recording."
        )
        return 1

    print(
        f"All {len(CASES)} messages were read by {model}. Note that where the two readers differ "
        f"above, neither is necessarily wrong -- the dates are re-parsed from the model's quoted "
        f"phrases either way, so no date came from the model."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
