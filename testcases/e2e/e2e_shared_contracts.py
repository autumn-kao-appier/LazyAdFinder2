"""Data contracts shared by E2E implementations, without platform expectations."""

from dataclasses import dataclass


@dataclass(frozen=True)
class E2ETestCase:
    key: str
    title: str
    phase: str
    priority: str


def definitions(*rows):
    """Build a checked registry local to one platform and integration mode."""
    result = {row.key: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("duplicate E2E TestCase key in one registry")
    return result
