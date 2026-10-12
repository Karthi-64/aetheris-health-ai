"""CSV helpers shared by exports.

Usage::

    from app.utils.csv import csv_safe

    writer.writerow([csv_safe(patient_name), amount])
"""

from __future__ import annotations

__all__ = ["CSV_FORMULA_PREFIXES", "csv_safe"]

#: Leading characters a spreadsheet treats as the start of a formula.
CSV_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    """Neutralise a cell that a spreadsheet would run as a formula.

    An export is opened in Excel or Sheets, and some of its cells hold text a
    user typed — a hospital name, a patient name. A value such as
    ``=HYPERLINK(...)`` would execute when the file is opened. Prefixing an
    apostrophe makes the application treat the cell as text (the OWASP CSV
    injection guidance); the apostrophe is not shown in the cell.

    Use it on text cells only. A number the server computed, such as a
    negative amount, must be written as it is so that it stays a number.

    :param value: The cell text.
    :returns: The text, prefixed with ``'`` if it starts with a formula character.
    """
    if value.startswith(CSV_FORMULA_PREFIXES):
        return f"'{value}"
    return value
