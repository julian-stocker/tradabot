"""Terminal rendering for the read-only Advisor.

Kept separate from the service so the report structure stays presentation-free
and can later feed a UI or JSON consumer unchanged.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from app.advisor.schemas import AdvisorReport, Metric, Section

_BILLIONS = 1e9
_LARGE_VALUE = 1e7


def _fmt(metric: Metric | None, *, pct: bool = False, scale: float = 1.0) -> str:
    if metric is None or not metric.available or metric.value is None:
        reason = "" if metric is None else (metric.unavailable_reason or "unavailable")
        return f"unavailable ({reason})" if reason else "unavailable"
    value = metric.value / scale
    return f"{value * 100:+.1f}%" if pct else f"{value:,.2f}"


def render(report: AdvisorReport, *, provenance: bool = False) -> str:
    """A readable terminal view. Missing data stays visible, never blank."""
    out: list[str] = [
        f"TRADABOT ADVISOR — {report.symbol}",
        f"As of: {report.as_of}",
        "",
        "COMPANY QUALITY",
    ]
    for section in report.company_quality:
        out.extend(_render_section(section, indent="    "))
    out += ["", f"VALUATION   [{report.valuation.confidence}]"]
    out.extend(_render_section(report.valuation, indent="  ", header=False))
    out += ["", f"MARKET POSITION   [{report.market_position.confidence}]"]
    for name, metric in report.market_position.metrics.items():
        out.append(f"  {name:<30}{_fmt(metric, pct=True)}")
    out.append("")
    out.extend(_render_risks(report))
    out += ["", "HORIZON DATA SUPPORT"]
    for horizon, detail in report.horizon_data_support.items():
        out.append(f"  {horizon:<12}{detail['data_support']}")
    out += ["", "DATA CONFIDENCE"]
    for section_name, level in report.confidence.items():
        out.append(f"  {section_name:<30}{level}")
    out += [
        "",
        "SUMMARY",
        f"  {report.summary}",
        "",
        f"INVESTMENT ASSESSMENT: {report.investment_assessment.reason}",
        f"  {report.disclaimer}",
    ]
    if provenance:
        out += ["", "PROVENANCE"]
        for section in (*report.company_quality, report.valuation):
            for name, metric in section.metrics.items():
                for source in metric.provenance:
                    out.append(
                        f"  {name:<26}{source.concept} {source.unit} "
                        f"{source.form or '-'} filed={source.filed} "
                        f"period={source.period_end} accn={source.accession}"
                    )
    return "\n".join(out)


def _render_section(section: Section, *, indent: str, header: bool = True) -> list[str]:
    lines: list[str] = []
    if header:
        lines.append(f"  {section.name}   [{section.confidence}]")
    width = 30 - len(indent) + 2
    for name, metric in section.metrics.items():
        big = metric.value is not None and abs(metric.value) > _LARGE_VALUE
        scale = _BILLIONS if big else 1.0
        suffix = "B" if big else ""
        lines.append(f"{indent}{name:<{width}}{_fmt(metric, scale=scale)}{suffix}")
    lines.extend(f"{indent}{k:<{width}}{v}" for k, v in section.labels.items())
    lines.extend(f"{indent}reason: {r}" for r in section.confidence_reasons)
    lines.extend(f"{indent}note: {n}" for n in section.notes)
    return lines


def _render_risks(report: AdvisorReport) -> list[str]:
    lines = ["RISKS"]
    found = [f"  [{c}] {i}" for c, items in report.risks.items() for i in items]
    lines.extend(found or ["  none identified from available data"])
    return lines


def to_json(report: AdvisorReport) -> str:
    payload: dict[str, Any] = asdict(report)
    return json.dumps(payload, indent=1, default=str)
