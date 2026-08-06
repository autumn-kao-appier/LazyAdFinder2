#!/usr/bin/env python3
"""Verdict contract shared by testcase evaluators and report generators.

Testcase expectations and validators are intentionally absent.  They will be
added one at a time after each testcase's correct standard has been reviewed.
This module currently defines only the three externally visible outcomes and
the structured result consumed by ``page.py``.
"""

import json
from dataclasses import asdict, dataclass
from enum import Enum
from operator import eq
from typing import Any, Callable, Optional


class Status(str, Enum):
    """The only outcomes a testcase may expose to a report."""

    BLOCKED = "BLOCKED"
    PASS = "PASS"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Verdict:
    """A JSON-serializable testcase result for ``page.py``.

    A blocked testcase may preserve observations and evidence when execution
    completed but an independent expected answer is still unavailable.  It
    must not claim an expected answer until a reviewer supplies one.
    """

    tc: str
    status: Status
    reason: str
    expected: Any = None
    actual: Any = None
    evidence: Optional[str] = None

    def __post_init__(self):
        if not self.tc.strip():
            raise ValueError("tc must not be empty")
        if self.status is Status.BLOCKED:
            if not self.reason.strip():
                raise ValueError("BLOCKED requires a concrete Round/environment reason")
            if self.expected is not None:
                raise ValueError("BLOCKED cannot claim an expected answer")
        elif not self.evidence or not self.evidence.strip():
            raise ValueError("PASS/FAILED requires an evidence reference")

    def to_dict(self):
        result = asdict(self)
        result["status"] = self.status.value
        try:
            json.dumps(result)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"Verdict {self.tc!r} contains values that cannot be written to verdicts.json"
            ) from exc
        return result


def blocked(tc: str, reason: str) -> Verdict:
    """Return BLOCKED when a Round or environment limitation prevented a run."""
    return Verdict(tc=tc, status=Status.BLOCKED, reason=reason)


def evaluate(
    tc: str,
    *,
    expected: Any,
    actual: Any,
    evidence: str,
    compare: Callable[[Any, Any], bool] = eq,
    reason: str = "",
) -> Verdict:
    """Compare an executed testcase and return PASS or FAILED.

    The default comparison is equality.  A testcase with a reviewed custom
    standard supplies its own ``compare(expected, actual)`` callable when that
    testcase is added.
    """
    passed = bool(compare(expected, actual))
    return Verdict(
        tc=tc,
        status=Status.PASS if passed else Status.FAILED,
        reason=reason,
        expected=expected,
        actual=actual,
        evidence=evidence,
    )
