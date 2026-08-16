"""Terminal rendering for monitoring. Presentation only.

The quiet case is rendered deliberately, not left as an empty screen. "Nothing
material changed" is the answer this engine gives most of the time, and it has
to look like a *result* -- with the count of what was examined and suppressed
behind it -- or a reader cannot tell it apart from a run that failed.
"""

from __future__ import annotations

import json
from typing import Any

from app.monitoring.digest import Digest
from app.monitoring.schemas import ChangeEvent, MonitoringRun

_MATERIALITY_MARK = {
    "CRITICAL": "!!",
    "SIGNIFICANT": " !",
    "NOTABLE": "  ",
    "ROUTINE": "  ",
}


def render_run(
    run: MonitoringRun, *, evidence: bool = False, limit: int | None = None
) -> str:
    """One monitoring pass as text.

    ``limit`` truncates the displayed list, never the run itself. Events are
    already ranked, so the first few are the ones that matter; earnings season
    puts fifty genuinely material changes into a single session and printing all
    of them is how a reader learns to skip the whole thing.
    """
    out: list[str] = [
        f"TRADABOT MONITOR — {run.as_of}",
        f"Examined {run.subjects_examined} subjects.",
        "",
    ]
    if run.quiet:
        out += [
            "NOTHING MATERIAL CHANGED.",
            "",
            f"  {run.suppressed_routine} changes were below their reporting threshold",
            f"  {run.suppressed_cooldown} were repeats inside their cooldown",
            f"  {run.suppressed_duplicate} were duplicates within this run",
        ]
    else:
        shown = run.events if limit is None else run.events[:limit]
        out.append(f"{len(run.events)} MATERIAL CHANGE(S), most important first")
        out.append("")
        for index, event in enumerate(shown, start=1):
            out.extend(_render_event(index, event, evidence=evidence))
        if len(shown) < len(run.events):
            out.append(f"  … {len(run.events) - len(shown)} more not shown")
        out += [
            "",
            f"Suppressed: {run.suppressed_routine} routine, "
            f"{run.suppressed_cooldown} in cooldown, "
            f"{run.suppressed_duplicate} duplicate.",
        ]
    out += ["", *(f"  {note}" for note in run.notes)]
    return "\n".join(out)


def _render_event(index: int, event: ChangeEvent, *, evidence: bool) -> list[str]:
    mark = _MATERIALITY_MARK.get(str(event.materiality), "  ")
    scope = event.scope.account or str(event.scope.kind)
    out = [
        f"{mark} {index}. [{event.materiality}] {event.kind} — {event.subject}"
        f"  ({scope}, confidence {event.confidence})",
        f"      {event.summary}",
        f"      {event.previous_state or 'no prior state'}  ->  {event.current_state}",
    ]
    if evidence:
        for item in event.evidence:
            threshold = (
                f", threshold {item.threshold}" if item.threshold is not None else ""
            )
            out.append(
                f"      · {item.measure}: {item.previous} -> {item.current}{threshold}"
            )
        for source in event.provenance:
            out.append(f"      · source: {source.source} as of {source.as_of}")
    out.append("")
    return out


def render_digest(digest: Digest) -> str:
    """A period summary as text."""
    out: list[str] = [
        f"TRADABOT MONITOR DIGEST — {digest.since} to {digest.until}",
        f"{digest.events_considered} reported event(s) in the period.",
        "",
    ]
    if digest.quiet:
        out += ["NOTHING MATERIAL CHANGED IN THIS PERIOD.", ""]
    for section in digest.sections:
        out.append(f"{section.title.upper()} — {section.question}")
        if section.empty:
            out.append("  (nothing)")
        for row in section.rows:
            if "risk" in row:
                out.append(f"  [{row['materiality']}] {row['risk']} — {row['subject']}")
                out.append(f"      {row['detail']}")
            else:
                account = f" [{row['account']}]" if row.get("account") else ""
                out.append(
                    f"  [{row['materiality']}] {row['kind']} — {row['subject']}{account}"
                )
                out.append(f"      {row['summary']}")
        if section.omitted:
            out.append(f"  … {section.omitted} more not shown")
        out.append("")
    out.extend(f"  {note}" for note in digest.notes)
    return "\n".join(out)


def run_to_json(run: MonitoringRun) -> str:
    payload: dict[str, Any] = run.as_dict()
    return json.dumps(payload, indent=1, default=str)


def digest_to_json(digest: Digest) -> str:
    payload: dict[str, Any] = digest.as_dict()
    return json.dumps(payload, indent=1, default=str)
