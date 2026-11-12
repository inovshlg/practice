"""The trade-management state machine shared by backtest and live engine."""
from __future__ import annotations


def actions_for_cross(pos_direction: int | None, cross_direction: int, in_window: bool) -> list[tuple]:
    """Given the current position direction (None = flat) and a new cross, return the actions.

    * opposite cross while in a position -> EXIT ('REVERSAL' if a new entry follows, else 'OPPOSITE_CROSS')
    * flat (or just exited) and the cross is inside the entry window -> ENTER in the cross direction
    Crosses alternate by construction, so a same-direction cross while in a position cannot occur;
    if it ever did it is ignored (no pyramiding).
    """
    acts: list[tuple] = []
    flat = pos_direction is None
    if not flat and cross_direction != pos_direction:
        acts.append(("EXIT", "REVERSAL" if in_window else "OPPOSITE_CROSS"))
        flat = True
    if flat and in_window:
        acts.append(("ENTER", cross_direction))
    return acts
