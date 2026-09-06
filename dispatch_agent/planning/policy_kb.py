"""Searching the written delivery policy, so an answer can cite the rule it came from.

The knowledge base is one 200-line Markdown file with a heading per rule and a stable ID on each
(`### WINDOW-1 - Three broad arrival windows`). At that size, a vector database would be more
moving parts than knowledge: embedding a file you could read end to end in a second buys nothing
and adds a service that can be down during a demo.

So this is keyword scoring over the rule blocks, and it says so. Three things make it good enough:

- **The vocabulary is small and fixed.** Customers ask about days, windows, regions, being home,
  and leaving food outside. Those words are in the rules, because the rules are about them.
- **Headings are worth more than bodies.** A rule titled "Somebody must be home" should win the
  question "why do you need someone at home?" outright, and it does.
- **Synonyms are declared, not guessed.** "Outside", "doorstep" and "unattended" are the customer's
  words for the same rule; the mapping is a table anyone can read and correct.

`search()` returns whole rule blocks, never fragments. A model answering from half a rule is how
you get a confident sentence the policy does not support -- and the point of retrieval here is that
the reply is traceable to a rule ID a judge can look up.

Nothing here writes anything. It reads one file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "delivery-policy.md"

# How many rules a reply may be built from. Enough for a question that spans two rules ("which day
# do you deliver to the West, and what times?"), few enough that the model is not handed the whole
# file and left to choose -- which is retrieval in name only.
MAX_RULES = 4

# A rule has to actually be about the question. Below this it is a word that happened to appear,
# and returning it invites an answer built on an unrelated rule.
MIN_SCORE = 2.0

# Words that carry no topic. Left out of scoring so "what are the delivery days" is not matched by
# every rule containing "the".
_STOPWORDS = frozenset(
    """a an and are as at be by can could do does for from has have how i if in is it its me my
    of on or our so that the their them there they this to was we what when where which who why
    will with would you your please tell about need any""".split()
)

# The customer's words for things the policy names differently. Declared rather than inferred:
# a table is auditable, and when a question misses, the fix is one line here.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "outside": ("unattended", "door", "attendance", "home"),
    "doorstep": ("unattended", "door", "attendance"),
    "unattended": ("unattended", "door", "attendance"),
    "porch": ("unattended", "door", "attendance"),
    "gate": ("unattended", "door", "attendance"),
    "neighbour": ("unattended", "neighbour", "attendance"),
    "neighbor": ("unattended", "neighbour", "attendance"),
    "concierge": ("unattended", "concierge", "attendance"),
    "locker": ("unattended", "locker", "attendance"),
    "security": ("unattended", "security", "attendance"),
    "leave": ("unattended", "attendance"),
    "drop": ("unattended", "attendance"),
    "home": ("home", "attendance", "present"),
    "in": (),  # a stopword that survives tokenising inside "leave it in the lobby"
    "timing": ("window", "windows", "arrival"),
    "timings": ("window", "windows", "arrival"),
    "time": ("window", "windows", "arrival"),
    "times": ("window", "windows", "arrival"),
    "slot": ("window", "windows"),
    "slots": ("window", "windows"),
    "hours": ("window", "windows", "arrival"),
    "day": ("day", "days", "friday", "saturday"),
    "days": ("day", "days", "friday", "saturday"),
    "area": ("region", "regions", "district"),
    "region": ("region", "regions", "district"),
    "zone": ("region", "regions", "district"),
    "west": ("west", "region", "saturday"),
    "east": ("east", "region", "friday"),
    "north": ("north", "region", "friday"),
    "south": ("south", "region", "friday"),
    "central": ("central", "region", "saturday"),
    "driver": ("driver", "route"),
    "cancel": ("cancel",),
    "miss": ("missed", "nobody"),
    "missed": ("missed", "nobody"),
    "late": ("late", "arrival", "window"),
    "spoil": ("spoils", "fresh", "safe"),
    "fresh": ("fresh", "food", "safe"),
}


@dataclass(frozen=True)
class Rule:
    """One `### ID - Title` block, whole."""

    id: str
    title: str
    topic: str
    text: str
    score: float = 0.0

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "topic": self.topic, "text": self.text}


def _read() -> str:
    """Read fresh every call. The file is tiny, and a cached copy would let the trace quote a rule
    the repository no longer contains -- which is worse than a millisecond of disk."""
    return POLICY_PATH.read_text(encoding="utf-8") if POLICY_PATH.exists() else ""


def rules() -> list[Rule]:
    """Every rule in the file, each with the `## topic` it sits under."""
    text = _read()
    if not text:
        return []

    found: list[Rule] = []
    topic = ""
    # One pass over headings. `##` sets the topic, `###` opens a rule that runs to the next
    # heading of either level.
    pattern = re.compile(r"^(##|###)\s+(.+?)\s*$", re.M)
    marks = list(pattern.finditer(text))
    for i, mark in enumerate(marks):
        level, heading = mark.group(1), mark.group(2)
        if level == "##":
            topic = heading.strip()
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[mark.end():end].strip()
        # "WINDOW-1 - Three broad arrival windows" -> the id and the title, separately.
        split = re.match(r"^([A-Z]+-\d+)\s*[-—–]+\s*(.*)$", heading)
        rule_id, title = (split.group(1), split.group(2)) if split else ("", heading)
        found.append(Rule(id=rule_id, title=title, topic=topic, text=body))
    return found


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", text.lower()) if w not in _STOPWORDS and len(w) > 1]


def _expand(question: str) -> set[str]:
    """The question's own words, plus the policy's words for the same things."""
    words = set(_tokens(question))
    for word in list(words):
        words.update(_SYNONYMS.get(word, ()))
    return words


def search(question: str, limit: int = MAX_RULES) -> list[Rule]:
    """The rules that answer this question, best first. Empty when nothing is close enough.

    Empty is a real answer and the caller must treat it as one: "the policy does not cover this"
    is the honest reply, and inventing one from the nearest unrelated rule is the failure this
    whole module exists to prevent.
    """
    wanted = _expand(question)
    if not wanted:
        return []

    scored: list[Rule] = []
    for rule in rules():
        title_words = set(_tokens(rule.title))
        body_words = set(_tokens(rule.text))
        topic_words = set(_tokens(rule.topic.replace("_", " ")))
        # A title match is the rule announcing it is about this; a body match is a mention. Weighted
        # so "why do you need someone at home?" lands on ATTEND-1 rather than on whichever long
        # rule happens to use the word "home" in passing.
        score = (
            2.0 * len(wanted & title_words)
            + 1.5 * len(wanted & topic_words)
            + 1.0 * len(wanted & body_words)
        )
        if score >= MIN_SCORE:
            scored.append(Rule(rule.id, rule.title, rule.topic, rule.text, score))

    # Score first, then rule ID -- so two equally relevant rules come back in the same order every
    # run, and a recorded demo replays identically.
    scored.sort(key=lambda r: (-r.score, r.id))
    return scored[:limit]
