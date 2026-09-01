"""Shared pieces of the ingest-refresh flow.

The CLI (``foray refresh``) and the API (``POST /api/refresh``) run two different
orchestrations of the same ingest sequence - the CLI prints to stdout and has a
coverage-wide ``--all`` mode, the API runs in a background thread and broadcasts SSE
progress - but the target vocabulary and the month-list parsing are identical, so they live
here where they can't drift apart. Folding the two orchestrations themselves into one caller
is tracked as the remaining half of issue #242 Part 1f.
"""

from __future__ import annotations

# The layers a refresh can warm, in ingest order. The API additionally accepts the
# pseudo-target ``"all"`` (every layer); the CLI expresses "everything" as an empty ``--with``.
REFRESH_LAYERS: tuple[str, ...] = ("mushrooms", "camps", "land", "dispersed", "trails")
REFRESH_TARGETS: frozenset[str] = frozenset({"all", *REFRESH_LAYERS})


def parse_month_list(months: str) -> list[int]:
    """Parse a comma-separated month list (e.g. ``"3,4,5"``) into ints, validating the range.

    Raises ``ValueError`` with a caller-friendly message on non-integer or out-of-range
    input. An empty/whitespace string yields ``[]`` so each caller can apply its own default
    (the current month for a point query, all twelve for a whole-year rollup).
    """
    try:
        values = [int(token) for token in months.split(",") if token.strip()]
    except ValueError:
        raise ValueError(f"months must be integers 1-12: {months!r}") from None
    if not all(1 <= month <= 12 for month in values):
        raise ValueError("months must be in 1-12")
    return values
