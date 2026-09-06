"""A memo of drive times between coordinate pairs, sitting underneath RoutingClient.

Why this exists: evaluating one booking means solving ~7 days (four candidate dates, each with a
baseline and a with-candidate solve). Without a cache that is up to 24 full matrix fetches per
booking -- billed per element under Google, and O(n^2) *sequential* HTTP calls under OneMap.
Neither is survivable in a live demo.

The decisive fact about this codebase is that `postal_code_to_coords` resolves a postal code to
one of Singapore's 28 district centroids. Including the depot, the entire coordinate universe of
the application is 29 points, i.e. 841 ordered pairs. Once warm, every solve in the demo is free.
That is also why the key is a coordinate pair and not a date, job or plan: drive time between two
fixed points does not change, so there is no invalidation to get wrong.

Rounding to 1e-5 degrees (~1 metre) makes the key stable against float noise while never merging
two genuinely distinct addresses.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dispatch_agent.config import settings
from dispatch_agent.db import current_connection
from dispatch_agent.models import Coordinates

Key = tuple[str, int, int, int, int]
Pair = tuple[Coordinates, Coordinates]

_PRECISION = 100_000  # 1e-5 degrees, ~1m

# How much redundant fetching one consolidated request may cost before it stops being worth the
# saved round trips. 2.0 is chosen to sit just above the cold-matrix ratio, n^2 / n(n-1), which
# tends to 1 -- and well below a day-plus-candidate's n^2 / 2n, which grows with the day.
_SINGLE_RECTANGLE_WASTE_FACTOR = 2.0


def _key(provider: str, origin: Coordinates, destination: Coordinates) -> Key:
    return (
        provider,
        round(origin.lat * _PRECISION),
        round(origin.lng * _PRECISION),
        round(destination.lat * _PRECISION),
        round(destination.lng * _PRECISION),
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS drive_time_cache (
    provider TEXT NOT NULL,
    o_lat INTEGER NOT NULL, o_lng INTEGER NOT NULL,
    d_lat INTEGER NOT NULL, d_lng INTEGER NOT NULL,
    minutes INTEGER NOT NULL, km REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (provider, o_lat, o_lng, d_lat, d_lng)
);
"""


class DriveTimeCache:
    """In-memory front, optional SQLite table behind it.

    The SQLite layer exists so a `uvicorn --reload` restart mid-demo does not re-bill the whole
    horizon. It is a pure cache: deleting the table costs nothing but a re-fetch, and it is never
    consulted for correctness -- only for cost.
    """

    def __init__(self) -> None:
        self._memory: dict[Key, dict] = {}
        self._loaded_from_disk = False
        self.hits = 0
        self.misses = 0
        self.elements_fetched = 0
        self.provider_requests = 0

    # -- persistence -----------------------------------------------------------

    def _db_path(self) -> Path:
        return Path(settings.db_path)

    def load(self) -> None:
        """Best-effort warm start from disk. A missing or unreadable cache table is not an error;
        it just means everything is a miss."""
        if self._loaded_from_disk:
            return
        self._loaded_from_disk = True
        path = self._db_path()
        if not path.exists():
            return
        try:
            active = current_connection()
            if active is not None:
                active.execute(SCHEMA)
                rows = active.execute(
                    "SELECT provider, o_lat, o_lng, d_lat, d_lng, minutes, km FROM drive_time_cache"
                ).fetchall()
            else:
                with sqlite3.connect(path) as conn:
                    conn.execute(SCHEMA)
                    rows = conn.execute(
                        "SELECT provider, o_lat, o_lng, d_lat, d_lng, minutes, km FROM drive_time_cache"
                    ).fetchall()
        except sqlite3.Error:
            return
        for provider, o_lat, o_lng, d_lat, d_lng, minutes, km in rows:
            self._memory[(provider, o_lat, o_lng, d_lat, d_lng)] = {"minutes": minutes, "km": km}

    def _persist(self, entries: list[tuple[Key, dict]]) -> None:
        if not entries:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            path = self._db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            active = current_connection()
            if active is not None:
                active.execute(SCHEMA)
                active.executemany(
                    "INSERT INTO drive_time_cache "
                    "(provider, o_lat, o_lng, d_lat, d_lng, minutes, km, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                    "minutes=excluded.minutes, km=excluded.km, fetched_at=excluded.fetched_at",
                    [(*k, v["minutes"], v["km"], now) for k, v in entries],
                )
            else:
                with sqlite3.connect(path) as conn:
                    conn.execute(SCHEMA)
                    conn.executemany(
                        "INSERT INTO drive_time_cache "
                        "(provider, o_lat, o_lng, d_lat, d_lng, minutes, km, fetched_at) "
                        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                        "minutes=excluded.minutes, km=excluded.km, fetched_at=excluded.fetched_at",
                        [(*k, v["minutes"], v["km"], now) for k, v in entries],
                    )
        except sqlite3.Error:
            pass  # a cache that cannot persist is still a working cache

    # -- lookup ----------------------------------------------------------------

    def peek(self, provider: str, origin: Coordinates, destination: Coordinates) -> dict | None:
        """Read without touching the counters. Assembling a matrix re-reads every pair that
        `split` already accounted for, and counting those again would report several hits per
        pair and make the stats useless for judging real provider cost."""
        self.load()
        return self._memory.get(_key(provider, origin, destination))

    def get(self, provider: str, origin: Coordinates, destination: Coordinates) -> dict | None:
        value = self.peek(provider, origin, destination)
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
        return value

    def put(self, provider: str, origin: Coordinates, destination: Coordinates, value: dict) -> None:
        key = _key(provider, origin, destination)
        self._memory[key] = value
        self._persist([(key, value)])

    def put_many(self, provider: str, entries: list[tuple[Coordinates, Coordinates, dict]]) -> None:
        keyed = [(_key(provider, o, d), v) for o, d, v in entries]
        self._memory.update(dict(keyed))
        self._persist(keyed)

    def split(self, provider: str, pairs: list[Pair]) -> tuple[int, list[Pair]]:
        """Partition `pairs` into a count of what we already know and a list of what must be
        fetched. Returns a count rather than the hit values because Coordinates is a Pydantic
        model and therefore unhashable -- callers read hits back through `peek` anyway."""
        self.load()
        misses: list[Pair] = []
        hit_count = 0
        for origin, destination in pairs:
            if self._memory.get(_key(provider, origin, destination)) is None:
                misses.append((origin, destination))
            else:
                hit_count += 1
        self.hits += hit_count
        self.misses += len(misses)
        return hit_count, misses

    # -- bookkeeping -----------------------------------------------------------

    def record_fetch(self, elements: int, requests_made: int = 1) -> None:
        self.elements_fetched += elements
        self.provider_requests += requests_made

    def stats(self) -> dict:
        return {
            "entries": len(self._memory),
            "hits": self.hits,
            "misses": self.misses,
            "elements_fetched": self.elements_fetched,
            "provider_requests": self.provider_requests,
        }

    def clear(self) -> None:
        """In-memory only -- tests want a cold cache without touching anyone's saved data."""
        self._memory.clear()
        self._loaded_from_disk = False
        self.hits = self.misses = self.elements_fetched = self.provider_requests = 0

    def warm(self, client, points: list[Coordinates]) -> None:
        """Pre-fetch every ordered pair among `points`. Called by the seed script so the demo
        never pays for drive times on camera."""
        client.matrix(points)


def cover_misses(misses: list[Pair]) -> list[tuple[list[Coordinates], list[Coordinates]]]:
    """Group cache misses into the fewest origins x destinations rectangles.

    Fetching misses one pair at a time would defeat the provider's batching entirely. Origins
    missing an identical set of destinations can share one request -- but that alone is not
    enough: on a cold n-point matrix every origin is missing *everything except itself*, so
    grouping by exact set yields n separate (1 x n-1) requests instead of one n x n. Hence the
    second step: when one big rectangle costs barely more than the exact grouping, take it,
    because request count dominates latency.

    The two shapes that matter:

    - cold cache over n points -> one n x n rectangle (today's behaviour, n^2 elements)
    - a known day plus one new candidate -> two rectangles ({new} x rest, rest x {new}),
      i.e. 2n elements rather than n^2, which is what makes candidate evaluation cheap
    """
    by_origin: dict[tuple[float, float], set[tuple[float, float]]] = defaultdict(set)
    coords: dict[tuple[float, float], Coordinates] = {}
    for origin, destination in misses:
        o_id = (origin.lat, origin.lng)
        d_id = (destination.lat, destination.lng)
        coords[o_id] = origin
        coords[d_id] = destination
        by_origin[o_id].add(d_id)

    grouped: dict[frozenset, list[tuple[float, float]]] = defaultdict(list)
    for o_id, destinations in by_origin.items():
        grouped[frozenset(destinations)].append(o_id)

    exact = [
        ([coords[o] for o in sorted(origins)], [coords[d] for d in sorted(destinations)])
        for destinations, origins in grouped.items()
    ]
    if len(exact) <= 1:
        return exact

    all_origins = sorted(by_origin)
    all_destinations = sorted({d for dests in by_origin.values() for d in dests})
    single_cost = len(all_origins) * len(all_destinations)
    exact_cost = sum(len(o) * len(d) for o, d in exact)

    # Collapsing to one request is worth a bounded amount of redundant fetching; past that the
    # extra elements cost more than the round trips they save. A cold n x n matrix lands just
    # inside this (n^2 vs n(n-1)); a day-plus-one-candidate lands well outside it (n^2 vs 2n).
    if single_cost <= exact_cost * _SINGLE_RECTANGLE_WASTE_FACTOR:
        return [([coords[o] for o in all_origins], [coords[d] for d in all_destinations])]
    return exact


MATRIX_CACHE = DriveTimeCache()
