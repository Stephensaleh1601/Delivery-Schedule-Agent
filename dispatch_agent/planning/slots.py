"""The three delivery windows, and the one place they are defined.

A window is a promise about ARRIVAL: the van turns up between 5 and 9. It is not an appointment,
and it is not a statement about when the delivery finishes -- see `config.arrival_cutoff` and
`solver._normalised_windows` for the two halves of that rule.

Defined here rather than inline because three separate things have to agree about them: the seed
that builds the routes, the insertion tool deciding which window a candidate arrival falls in, and
the wording a customer reads. Three copies of "afternoon is 2 to 5" is three chances to drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as Time

from dispatch_agent.models import TimeWindow


@dataclass(frozen=True)
class DeliverySlot:
    """One broad arrival window, with the name a customer would use for it."""

    name: str
    label: str
    start: Time
    end: Time

    @property
    def window(self) -> TimeWindow:
        return TimeWindow(start=self.start, end=self.end)

    def contains(self, moment: Time) -> bool:
        """Half-open: the boundary belongs to the later slot, so 14:00 is afternoon, not morning.

        Closed on both ends would put 2pm in two windows at once, and the insertion tool would
        then produce two candidates for one arrival and call them different choices.
        """
        return self.start <= moment < self.end


MORNING = DeliverySlot("morning", "Morning", Time(10, 0), Time(14, 0))
AFTERNOON = DeliverySlot("afternoon", "Afternoon", Time(14, 0), Time(17, 0))
EVENING = DeliverySlot("evening", "Evening", Time(17, 0), Time(21, 0))

SLOTS: tuple[DeliverySlot, ...] = (MORNING, AFTERNOON, EVENING)
BY_NAME: dict[str, DeliverySlot] = {slot.name: slot for slot in SLOTS}


def slot_containing(moment: Time) -> DeliverySlot | None:
    """Which window an arrival falls in, or None before 10am / at 21:00 and after.

    None is a real answer: an arrival outside every window cannot be offered as one, and the
    caller drops the candidate rather than rounding it into the nearest slot.
    """
    for slot in SLOTS:
        if slot.contains(moment):
            return slot
    return None
