"""Canonical contract for one bounded spreadsheet-entry action."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Any


MAX_GRID_ROWS = 8
MAX_GRID_COLUMNS = 8
MAX_GRID_CELL_CHARACTERS = 80
MAX_GRID_CHARACTERS = 240


class SpreadsheetGridError(ValueError):
    """The proposed grid is unsafe or cannot be represented exactly."""


@dataclass(frozen=True)
class SpreadsheetGrid:
    """Validated immutable spreadsheet data plus its logical receipt shape."""

    rows: tuple[tuple[str, ...], ...]

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return len(self.rows[0])

    @property
    def cell_count(self) -> int:
        return self.row_count * self.column_count

    @property
    def character_count(self) -> int:
        return sum(len(value) for row in self.rows for value in row)

    @property
    def navigation_count(self) -> int:
        tabs = self.row_count * max(0, self.column_count - 1)
        enters = self.row_count
        homes = max(0, self.row_count - 1)
        return tabs + enters + homes

    def payload(self, *, cell_limit: int | None = None) -> str:
        """Return a stable TSV-like logical payload for hashes and receipts."""

        remaining = self.cell_count if cell_limit is None else max(0, cell_limit)
        logical_rows: list[str] = []
        for row in self.rows:
            values = tuple(islice(row, remaining))
            if not values:
                break
            logical_rows.append("\t".join(values))
            remaining -= len(values)
        return "\n".join(logical_rows)

    def issued_character_count(self, cell_count: int) -> int:
        values = (value for row in self.rows for value in row)
        return sum(len(value) for value in islice(values, max(0, cell_count)))


def validate_spreadsheet_grid(
    rows: Any,
    *,
    max_characters: int = MAX_GRID_CHARACTERS,
) -> SpreadsheetGrid:
    """Validate and normalize the grid once for models and HID execution."""

    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_GRID_ROWS:
        raise SpreadsheetGridError("requires 1 to 8 rows")
    if any(not isinstance(row, list) or not row for row in rows):
        raise SpreadsheetGridError("rows must have the same number of columns")
    widths = {len(row) for row in rows}
    if len(widths) != 1:
        raise SpreadsheetGridError("rows must have the same number of columns")
    column_count = next(iter(widths))
    if not 1 <= column_count <= MAX_GRID_COLUMNS:
        raise SpreadsheetGridError("requires 1 to 8 columns")
    values = [value for row in rows for value in row]
    if any(
        not isinstance(value, str)
        or not value
        or len(value) > MAX_GRID_CELL_CHARACTERS
        for value in values
    ):
        raise SpreadsheetGridError("cells must contain 1 to 80 characters")
    if sum(map(len, values)) > max_characters:
        raise SpreadsheetGridError(
            f"contains more than {max_characters} typed characters"
        )
    if any(
        ord(character) < 32 or ord(character) == 127
        for value in values
        for character in value
    ):
        raise SpreadsheetGridError("cells cannot contain control characters")
    return SpreadsheetGrid(
        rows=tuple(tuple(value for value in row) for row in rows)
    )
