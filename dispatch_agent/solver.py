"""Sequencing: a single-vehicle TSP with time windows, solved with OR-Tools' routing library.

This is a time-limited guided local search, NOT an exact method. At the size this runs at (a
handful to a few dozen jobs a day) it reliably finds good routes, and an infeasible model is
reported honestly rather than approximated -- but do not describe its output as optimal.

One vehicle: this sequences one installer/driver's day, not a multi-vehicle fleet.

Two properties matter more than route quality here:

- A customer with several disjoint windows is never scheduled in the gap between them.
- A CONFIRMED customer's locked window is never violated. If the locked appointments cannot all
  be honoured, that is a LockedPlanInfeasibleError for a human to resolve -- never something the
  solver silently fixes by moving someone who was already promised a time.
"""
from __future__ import annotations

from datetime import date as Date, time as Time

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from dispatch_agent.config import settings
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.models import Coordinates, DaySequence, JobRecord, StopAssignment, TimeWindow

# Node windows as (start, end) minutes since midnight. A node carries a list of these: one entry
# for a simple window, several for a customer with disjoint availability.
NodeWindows = list[tuple[int, int]]


class UnsolvableDayError(Exception):
    """Raised when no ordering can satisfy every job's availability window."""


class LockedPlanInfeasibleError(UnsolvableDayError):
    """The set of confirmed, locked windows is itself unsatisfiable.

    Callers must escalate this to a coordinator exception. It must never be resolved by moving a
    locked job -- that is precisely the promise the lock exists to keep.
    """

    def __init__(
        self,
        message: str,
        delivery_date: Date,
        locked_job_ids: list[str],
        blocking_job_id: str | None = None,
    ):
        super().__init__(message)
        self.delivery_date = delivery_date
        self.locked_job_ids = locked_job_ids
        self.blocking_job_id = blocking_job_id


def _minutes_since_midnight(t: Time) -> int:
    return t.hour * 60 + t.minute


def _time_from_minutes(minutes: int) -> Time:
    minutes %= 24 * 60
    return Time(hour=minutes // 60, minute=minutes % 60)


def _effective_windows(job: JobRecord) -> list[TimeWindow]:
    """The windows the solver is actually allowed to use for this job.

    A locked window is the job's SOLE option. Falling back to `availability` for a confirmed job
    would let a replan quietly move the customer to a different time they once said they were
    free -- which is exactly what "we never move a confirmed appointment" forbids.
    """
    if job.is_locked:
        return [job.locked_window]
    return job.availability


def _normalised_windows(job: JobRecord, day_start: int, day_end: int, require_fit: bool) -> NodeWindows:
    """Clamp a job's windows to the working day, optionally reserve room for the service itself,
    then sort and merge touching windows.

    The merge matters: RemoveInterval's bounds are inclusive, so two windows that abut (12:00-13:00
    and 13:00-14:00) would otherwise produce RemoveInterval(781, 779) -- an inverted, meaningless
    range. Merging first guarantees every gap we punch out is real and non-empty.
    """
    raw: NodeWindows = []
    for window in _effective_windows(job):
        start = max(day_start, _minutes_since_midnight(window.start))
        end = min(day_end, _minutes_since_midnight(window.end))
        if require_fit:
            # CumulVar is the ARRIVAL time, so reserving the service duration here is what makes
            # "finishes inside the window" true rather than merely "starts inside it".
            end -= job.duration_minutes
        if end >= start:
            raw.append((start, end))

    if not raw:
        # Whose window this is matters to whoever reads the message: before confirmation it is the
        # customer's own availability, afterwards it is the narrow window WE promised them.
        whose = "the window we promised" if job.is_locked else f"{job.customer_name}'s availability"
        raise UnsolvableDayError(
            f"{whose} cannot fit {job.customer_name}'s {job.duration_minutes}-minute job "
            f"inside working hours"
        )

    raw.sort()
    merged: NodeWindows = [raw[0]]
    for start, end in raw[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def sequence_day(
    jobs: list[JobRecord],
    delivery_date: Date,
    depot: Coordinates,
    routing_client: RoutingClient | None = None,
    matrix: list[list[int]] | None = None,
    time_limit_seconds: int | None = None,
) -> DaySequence:
    """Order `jobs` (all for the same day) into a single route that fits every availability
    window, starting and ending at `depot`.

    `matrix` lets a caller supply a precomputed drive-time matrix over `[depot] + job coords`,
    skipping the routing call entirely -- candidate evaluation solves the same day many times
    and must not re-fetch drive times for each one.

    Raises LockedPlanInfeasibleError if confirmed appointments cannot all be honoured, and
    UnsolvableDayError for any other infeasible day.
    """
    if not jobs:
        return DaySequence(
            delivery_date=delivery_date, stops=[], total_drive_minutes=0, return_drive_minutes=0
        )

    points = [depot] + [j.address.coordinates for j in jobs]
    if any(p is None for p in points):
        raise ValueError("every job must be geocoded before sequencing")

    if matrix is None:
        routing_client = routing_client or RoutingClient()
        matrix = routing_client.matrix(points)

    day_start = _minutes_since_midnight(settings.work_day_start)
    day_end = _minutes_since_midnight(settings.work_day_end)
    limit = time_limit_seconds or settings.solver_time_limit_seconds

    # node 0 is the depot; nodes 1..n are jobs[0..n-1]
    windows: list[NodeWindows] = [[(day_start, day_end)]]
    for job in jobs:
        windows.append(
            _normalised_windows(job, day_start, day_end, settings.require_service_within_window)
        )

    solution, model = _solve(jobs, matrix, windows, day_start, day_end, limit)
    if solution is not None:
        return _extract(jobs, matrix, delivery_date, solution, model)

    raise _diagnose(jobs, matrix, windows, delivery_date, day_start, day_end, limit)


def _solve(
    jobs: list[JobRecord],
    matrix: list[list[int]],
    windows: list[NodeWindows],
    day_start: int,
    day_end: int,
    limit: int,
):
    """Build and solve one model. Returns (solution|None, model_bits) -- OR-Tools hands back
    None for an infeasible model rather than raising, including when a node's domain has been
    emptied by RemoveInterval."""
    manager = pywrapcp.RoutingIndexManager(len(matrix), 1, 0)
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

    for node, node_windows in enumerate(windows):
        cumul = time_dimension.CumulVar(manager.NodeToIndex(node))
        cumul.SetRange(node_windows[0][0], node_windows[-1][1])
        # Punch out the gaps between disjoint windows. This is a domain reduction on the
        # variable, not a constraint object -- do not wrap it in solver().Add(). It must happen
        # after GetDimensionOrDie and before SolveWithParameters, which closes the model.
        # Bounds are INCLUSIVE, hence the +1/-1 so arrival exactly on a window edge stays legal.
        for (_, previous_end), (next_start, _) in zip(node_windows, node_windows[1:]):
            cumul.RemoveInterval(previous_end + 1, next_start - 1)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.FromSeconds(limit)

    solution = routing.SolveWithParameters(search_parameters)
    return solution, (manager, routing, time_dimension)


def _extract(jobs, matrix, delivery_date: Date, solution, model) -> DaySequence:
    manager, routing, time_dimension = model
    stops: list[StopAssignment] = []
    index = routing.Start(0)
    sequence_index = 0
    previous_node = 0

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
                    drive_minutes_from_prev=matrix[previous_node][node],
                )
            )
            sequence_index += 1
            previous_node = node
        index = solution.Value(routing.NextVar(index))

    return DaySequence(
        delivery_date=delivery_date,
        stops=stops,
        total_drive_minutes=sum(s.drive_minutes_from_prev for s in stops),
        # The drive home. Omitting it made a distant last stop look free.
        return_drive_minutes=matrix[previous_node][0] if stops else 0,
    )


def _diagnose(
    jobs: list[JobRecord],
    matrix: list[list[int]],
    windows: list[NodeWindows],
    delivery_date: Date,
    day_start: int,
    day_end: int,
    limit: int,
) -> UnsolvableDayError:
    """Work out *why* a day is infeasible, and in particular whether the confirmed appointments
    are to blame. Only runs after a failed solve, so the happy path pays nothing for it.

    Cheap only because the drive-time matrix is already in hand: every probe below re-solves the
    same model with different domains and makes no routing calls.
    """
    locked_indices = [i for i, job in enumerate(jobs) if job.is_locked]
    locked_ids = [jobs[i].id for i in locked_indices]

    if not locked_indices:
        return UnsolvableDayError(
            f"no ordering of {len(jobs)} jobs on {delivery_date} satisfies every availability window"
        )

    # Relax every unlocked job to the whole working day, keeping the locks hard. If that solves,
    # the promises are mutually satisfiable and an unconfirmed job is what cannot be fitted.
    def relaxed(skip: int | None = None) -> list[NodeWindows]:
        out: list[NodeWindows] = [windows[0]]
        for i in range(len(jobs)):
            keep_lock = i in locked_indices and i != skip
            out.append(windows[i + 1] if keep_lock else [(day_start, day_end)])
        return out

    solution, _ = _solve(jobs, matrix, relaxed(), day_start, day_end, limit)
    if solution is not None:
        unconfirmed = [j.customer_name for i, j in enumerate(jobs) if i not in locked_indices]
        return UnsolvableDayError(
            f"{delivery_date}: the confirmed appointments all fit, but "
            f"{', '.join(unconfirmed)} cannot be added to the day"
        )

    # The locks themselves conflict. Name the one that unblocks the day, if we can do so cheaply.
    blocking_job_id = None
    if len(locked_indices) <= 8:
        for i in locked_indices:
            solution, _ = _solve(jobs, matrix, relaxed(skip=i), day_start, day_end, limit)
            if solution is not None:
                blocking_job_id = jobs[i].id
                break

    return LockedPlanInfeasibleError(
        f"{delivery_date}: no route satisfies all {len(locked_ids)} confirmed appointment windows",
        delivery_date=delivery_date,
        locked_job_ids=locked_ids,
        blocking_job_id=blocking_job_id,
    )
