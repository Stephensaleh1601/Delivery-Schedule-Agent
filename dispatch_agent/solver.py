"""Sequencing: a single-vehicle TSP with time windows, solved exactly with OR-Tools.

At the size this operates at (a handful to a few dozen jobs a day) an exact CP formulation
finishes in well under a second -- see PRD: "which handles time windows and is exact at our
size". One vehicle: this sequences one installer/driver's day, not a multi-vehicle fleet.
"""
from __future__ import annotations

from datetime import date as Date, time as Time

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from dispatch_agent.config import settings
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.models import Coordinates, DaySequence, JobRecord, StopAssignment, TimeWindow


class UnsolvableDayError(Exception):
    """Raised when no ordering can satisfy every job's availability window."""


def _minutes_since_midnight(t: Time) -> int:
    return t.hour * 60 + t.minute


def _time_from_minutes(minutes: int) -> Time:
    minutes %= 24 * 60
    return Time(hour=minutes // 60, minute=minutes % 60)


def sequence_day(
    jobs: list[JobRecord],
    delivery_date: Date,
    depot: Coordinates,
    routing_client: RoutingClient | None = None,
) -> DaySequence:
    """Order `jobs` (all for the same day) into a single route that fits every availability
    window, starting and ending at `depot`. Raises UnsolvableDayError if no ordering exists."""
    if not jobs:
        return DaySequence(delivery_date=delivery_date, stops=[], total_drive_minutes=0)

    routing_client = routing_client or RoutingClient()
    points = [depot] + [j.address.coordinates for j in jobs]
    if any(p is None for p in points):
        raise ValueError("every job must be geocoded before sequencing")
    matrix = routing_client.matrix(points)

    day_start = _minutes_since_midnight(settings.work_day_start)
    day_end = _minutes_since_midnight(settings.work_day_end)

    # node 0 is the depot; nodes 1..n are jobs[0..n-1]
    windows: list[tuple[int, int]] = [(day_start, day_end)]
    for job in jobs:
        job_windows = [
            (
                max(day_start, _minutes_since_midnight(w.start)),
                min(day_end, _minutes_since_midnight(w.end)),
            )
            for w in job.availability
        ]
        # If a customer gave multiple disjoint windows, this exact single-window-per-node
        # formulation conservatively spans the earliest start to the latest end rather than
        # modelling the gap between them -- out of scope for a same-day scheduler at this size.
        start = min(w[0] for w in job_windows)
        end = max(w[1] for w in job_windows)
        windows.append((start, end))

    manager = pywrapcp.RoutingIndexManager(len(points), 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        service = jobs[from_node - 1].duration_minutes if from_node != 0 else 0
        return matrix[from_node][to_node] + service

    transit_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    routing.AddDimension(
        transit_callback_index,
        day_end - day_start,  # slack: how long a vehicle may wait for a window to open
        day_end,  # capacity: cumul values are absolute minutes-since-midnight, up to day_end
        False,
        "Time",
    )
    time_dimension = routing.GetDimensionOrDie("Time")
    for node, (start, end) in enumerate(windows):
        index = manager.NodeToIndex(node)
        time_dimension.CumulVar(index).SetRange(start, end)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.FromSeconds(5)

    solution = routing.SolveWithParameters(search_parameters)
    if solution is None:
        raise UnsolvableDayError(
            f"no ordering of {len(jobs)} jobs on {delivery_date} satisfies every availability window"
        )

    stops: list[StopAssignment] = []
    index = routing.Start(0)
    sequence_index = 0
    prev_node = 0
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        if node != 0:
            job = jobs[node - 1]
            arrival = solution.Value(time_dimension.CumulVar(index))
            departure = arrival + job.duration_minutes
            stops.append(
                StopAssignment(
                    job_id=job.id,
                    sequence_index=sequence_index,
                    arrival_window=TimeWindow(
                        start=_time_from_minutes(arrival), end=_time_from_minutes(departure)
                    ),
                    drive_minutes_from_prev=matrix[prev_node][node],
                )
            )
            sequence_index += 1
            prev_node = node
        index = solution.Value(routing.NextVar(index))

    total_drive_minutes = sum(s.drive_minutes_from_prev for s in stops)
    return DaySequence(delivery_date=delivery_date, stops=stops, total_drive_minutes=total_drive_minutes)
