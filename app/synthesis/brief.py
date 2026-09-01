"""A research brief with no model involved.

This exists to answer a question honestly before any money is spent: **how much
of the value is reachable deterministically?** If a template over the same
evidence produces most of what a synthesis would, then a model is a cost rather
than a capability, and the right conclusion is to say so.

It produces a real :class:`~app.synthesis.contract.ResearchSynthesis` -- the
same type a provider would return, passing the same validator -- so the
fallback is not a degraded mode bolted on later. It is the baseline the model
has to beat, and the thing that renders when no model is available.

What it can and cannot do
-------------------------
It restates evidence accurately and connects two dimensions where the
connection is a fixed rule someone wrote in advance. It cannot notice that a
particular combination is unusual, cannot form a question nobody anticipated,
and cannot tell which of eleven true statements is the one worth reading. Those
are the gaps a model would have to fill, and naming them precisely is the point
of building this first.
"""

from __future__ import annotations

from typing import Final

from app.synthesis.contract import (
    MAX_PER_TYPE,
    SYNTHESIS_SCHEMA_VERSION,
    ClaimType,
    ModelMetadata,
    ResearchSynthesis,
    SynthesisClaim,
    SynthesisConfidence,
)
from app.synthesis.evidence import EvidenceItem, EvidencePacket

BRIEF_VERSION: Final = "18.0.0"

HIGH_PERCENTILE: Final = 75.0
LOW_PERCENTILE: Final = 25.0
MID_PERCENTILE: Final = 50.0
RICH_MULTIPLE_PERCENTILE: Final = 0.75
"""Bands used only to choose wording, never to grade a company. "Near the top
of its own range" is a description of where a number sits; it carries no view
about whether that is good."""

MAX_BRIEF_CLAIMS: Final = 12
MAX_PEER_CLAIMS: Final = 2
MIN_BLOCKS_HIGH: Final = 4
MIN_BLOCKS_MEDIUM: Final = 2

_MOVEMENT: Final[dict[str, tuple[str, str]]] = {
    "ratio": ("widened", "narrowed"),
    "shares": ("increased", "decreased"),
}


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _describe_change(item: EvidenceItem) -> str | None:
    """One sentence about a trajectory, in the metric's own unit."""
    value = item.value
    if not isinstance(value, dict):
        return None
    unit = item.unit or ""
    metric = item.evidence_id.split(".")[1]
    window = item.evidence_id.rsplit(".", 1)[-1]
    if unit == "ratio":
        return (
            f"{metric.replace('_', ' ')} moved from {_percent(value['from'])} to "
            f"{_percent(value['to'])} over {window}, a change of "
            f"{value['absolute']:+.1f} percentage points"
        )
    annualised = value.get("annualised")
    if annualised is not None:
        return (
            f"{metric.replace('_', ' ')} moved from {value['from']:,.0f} to "
            f"{value['to']:,.0f} over {window}, {annualised * 100:+.1f}% a year"
        )
    return (
        f"{metric.replace('_', ' ')} moved from {value['from']:,.0f} to "
        f"{value['to']:,.0f} over {window}"
    )


class ResearchBriefBuilder:
    """Turns a packet into a valid synthesis using fixed rules only.

    Every sentence is a template filled from one or two evidence items, and
    every claim carries the identifiers it was filled from -- so the brief
    satisfies the same evidence-link requirement a model's output must.
    """

    def build(self, packet: EvidencePacket) -> ResearchSynthesis:
        # Ordered by what a reader most needs, then trimmed per type. The
        # validator caps each claim type, and this builder is held to exactly
        # the contract a model would be -- its first draft produced seven
        # FACT_SUMMARY claims against a limit of four and was rejected.
        claims = _within_budget(
            [
                *self._trajectory_claims(packet),
                *self._development_claims(packet),
                *self._own_history_claims(packet),
                *self._peer_claims(packet),
                *self._tension_claims(packet),
                *self._uncertainty_claims(packet),
                *self._monitoring_claims(packet),
            ]
        )

        return ResearchSynthesis(
            company_key=packet.identity.company_key,
            as_of=packet.as_of,
            summary=self._summary(packet, claims),
            claims=tuple(claims[:MAX_BRIEF_CLAIMS]),
            confidence=self._confidence(packet),
            limitations=packet.limitations,
            metadata=ModelMetadata(
                provider="deterministic",
                model=f"research-brief-{BRIEF_VERSION}",
                schema_version=SYNTHESIS_SCHEMA_VERSION,
                packet_version=packet.version,
                packet_hash=packet.packet_hash,
                template_version=BRIEF_VERSION,
            ),
        )

    # ---------------------------------------------------------------- claims
    def _trajectory_claims(self, packet: EvidencePacket) -> list[SynthesisClaim]:
        out: list[SynthesisClaim] = []
        for index, item in enumerate(packet.trajectory[:3]):
            text = _describe_change(item)
            if text is None:
                continue
            out.append(
                SynthesisClaim(
                    claim_id=f"c.traj.{index}",
                    claim_type=ClaimType.FACT_SUMMARY,
                    text=text[0].upper() + text[1:] + ".",
                    evidence_ids=(item.evidence_id,),
                )
            )
        return out

    def _own_history_claims(self, packet: EvidencePacket) -> list[SynthesisClaim]:
        out: list[SynthesisClaim] = []
        for index, item in enumerate(packet.own_history[:2]):
            if not isinstance(item.value, (int, float)):
                continue
            where = (
                "near the top of"
                if item.value >= HIGH_PERCENTILE
                else ("near the bottom of" if item.value <= LOW_PERCENTILE else "in the middle of")
            )
            metric = item.evidence_id.split(".", 1)[1].replace("_", " ")
            out.append(
                SynthesisClaim(
                    claim_id=f"c.own.{index}",
                    claim_type=ClaimType.FACT_SUMMARY,
                    text=(
                        f"Current {metric} sits {where} its own recorded range "
                        f"({item.value:.0f}th percentile over {item.period})."
                    ),
                    evidence_ids=(item.evidence_id,),
                    temporal_scope="CURRENT",
                )
            )
        return out

    def _peer_claims(self, packet: EvidencePacket) -> list[SynthesisClaim]:
        out: list[SynthesisClaim] = []
        for index, item in enumerate(packet.peer_context):
            value = item.value
            if not isinstance(value, dict) or len(out) >= MAX_PEER_CLAIMS:
                continue
            percentile = value["percentile"]
            if LOW_PERCENTILE < percentile < HIGH_PERCENTILE:
                # Only the tails are worth a sentence; the middle says nothing.
                continue
            side = "above" if percentile >= HIGH_PERCENTILE else "below"
            out.append(
                SynthesisClaim(
                    claim_id=f"c.peer.{index}",
                    claim_type=ClaimType.INTERPRETATION,
                    text=(
                        f"{item.label} places the company {side} most of its industry "
                        f"group on this measure ({percentile:.0f}th percentile; "
                        f"{item.detail})."
                    ),
                    evidence_ids=(item.evidence_id,),
                    temporal_scope="CURRENT",
                )
            )
        return out

    def _development_claims(self, packet: EvidencePacket) -> list[SynthesisClaim]:
        out: list[SynthesisClaim] = []
        for index, item in enumerate(packet.developments[:2]):
            value = item.value if isinstance(item.value, dict) else {}
            items = ", ".join(f"Item {i}" for i in value.get("items") or []) or value.get(
                "form", ""
            )
            out.append(
                SynthesisClaim(
                    claim_id=f"c.dev.{index}",
                    claim_type=ClaimType.FACT_SUMMARY,
                    text=(
                        f"{item.label} was disclosed on {item.period} "
                        f"({value.get('form', '')} {items}; materiality "
                        f"{str(value.get('materiality', '')).lower()})."
                    ),
                    evidence_ids=(item.evidence_id,),
                    temporal_scope="CURRENT",
                )
            )
        return out

    def _tension_claims(self, packet: EvidencePacket) -> list[SynthesisClaim]:
        """The one cross-dimensional rule a template can state in advance.

        A margin moving one way while the multiple moves the other is worth a
        sentence, and it is worth exactly one sentence -- which is the limit of
        what a fixed rule can notice.
        """
        margin = next(
            (i for i in packet.trajectory if i.evidence_id.startswith("traj.operating_margin")),
            None,
        )
        multiple = next(
            (i for i in packet.market_context if i.evidence_id == "val.ps_percentile_own_history"),
            None,
        )
        if margin is None or multiple is None or not isinstance(margin.value, dict):
            return []
        if not isinstance(multiple.value, (int, float)):
            return []
        widened = margin.value["absolute"] > 0
        rich = multiple.value >= RICH_MULTIPLE_PERCENTILE
        if widened == rich:
            return []
        return [
            SynthesisClaim(
                claim_id="c.tension.0",
                claim_type=ClaimType.TENSION,
                text=(
                    f"Operating margin {'widened' if widened else 'narrowed'} over the "
                    f"window shown while the price-to-sales multiple sits at the "
                    f"{multiple.value * 100:.0f}th percentile of its own history."
                ),
                evidence_ids=(margin.evidence_id, multiple.evidence_id),
            )
        ]

    def _uncertainty_claims(self, packet: EvidencePacket) -> list[SynthesisClaim]:
        out: list[SynthesisClaim] = []
        for index, conflict in enumerate(packet.conflicts[:1]):
            out.append(
                SynthesisClaim(
                    claim_id=f"c.conflict.{index}",
                    claim_type=ClaimType.TENSION,
                    text=f"Two sources describe this differently: {conflict.detail}.",
                    evidence_ids=(conflict.evidence_a, conflict.evidence_b),
                )
            )
        material = [
            o
            for o in packet.omissions
            if str(o.reason) in ("SECTOR_MODEL_REQUIRED", "SOURCE_LIMITATION", "NO_COVERAGE")
        ]
        for index, omission in enumerate(material[:1]):
            anchor = next(iter(packet.evidence_ids), None)
            if anchor is None:
                continue
            out.append(
                SynthesisClaim(
                    claim_id=f"c.gap.{index}",
                    claim_type=ClaimType.UNCERTAINTY,
                    text=(
                        f"{omission.label} is not available: {omission.detail or omission.reason}."
                    ),
                    evidence_ids=(anchor,),
                    temporal_scope="CURRENT",
                )
            )
        return out

    def _monitoring_claims(self, packet: EvidencePacket) -> list[SynthesisClaim]:
        """Observable conditions in the company's own reported figures.

        Never a price level and never a trigger -- a question whose answer
        arrives in the next filing, not on a chart.
        """
        margin = next(
            (i for i in packet.own_history if i.evidence_id == "own.operating_margin"), None
        )
        if margin is None or not isinstance(margin.value, (int, float)):
            return []
        return [
            SynthesisClaim(
                claim_id="c.monitor.0",
                claim_type=ClaimType.MONITORING_QUESTION,
                text=(
                    "Whether operating margin in the next filed quarter stays "
                    f"{'above' if margin.value >= MID_PERCENTILE else 'below'} the midpoint of its "
                    f"own recorded range."
                ),
                evidence_ids=(margin.evidence_id,),
                temporal_scope="CURRENT",
            )
        ]

    # --------------------------------------------------------------- summary
    def _summary(self, packet: EvidencePacket, claims: list[SynthesisClaim]) -> str:
        name = packet.identity.company_name
        counts = {
            "trajectory": len(packet.trajectory),
            "peer": len(packet.peer_context),
            "developments": len(packet.developments),
        }
        if not claims:
            reason = packet.omissions[0].detail if packet.omissions else "no evidence available"
            return f"No research brief for {name}: {reason}."
        return (
            f"{name} as of {packet.as_of}: {counts['trajectory']} trajectory measure(s), "
            f"{counts['peer']} peer comparison(s) and {counts['developments']} current "
            f"filing(s) on file. The statements below restate that evidence and connect "
            f"it only where a fixed rule applies."
        )

    def _confidence(self, packet: EvidencePacket) -> SynthesisConfidence:
        """How complete the evidence was. Not a view about the shares."""
        blocks = sum(
            1
            for group in (
                packet.fundamentals,
                packet.trajectory,
                packet.peer_context,
                packet.market_context,
                packet.developments,
            )
            if group
        )
        unresolved = sum(1 for c in packet.conflicts if str(c.status) == "UNRESOLVED")
        if blocks >= MIN_BLOCKS_HIGH and not unresolved:
            return SynthesisConfidence.HIGH
        if blocks >= MIN_BLOCKS_MEDIUM:
            return SynthesisConfidence.MEDIUM
        return SynthesisConfidence.LOW


def _within_budget(claims: list[SynthesisClaim]) -> list[SynthesisClaim]:
    """The first N of each type, in the order they were offered.

    Trimming by type rather than overall keeps one dimension from crowding out
    the rest: a company with five trajectory measures and one development
    should still have the development stated.
    """
    kept: list[SynthesisClaim] = []
    seen: dict[ClaimType, int] = {}
    for claim in claims:
        count = seen.get(claim.claim_type, 0)
        if count >= MAX_PER_TYPE:
            continue
        seen[claim.claim_type] = count + 1
        kept.append(claim)
    return kept[:MAX_BRIEF_CLAIMS]


def build_brief(packet: EvidencePacket) -> ResearchSynthesis:
    return ResearchBriefBuilder().build(packet)


def as_text(synthesis: ResearchSynthesis) -> str:
    """A compact rendering, for a CLI or a card."""
    lines = [synthesis.summary, ""]
    for claim in synthesis.claims:
        lines.append(f"[{claim.claim_type}] {claim.text}")
        lines.append(f"    evidence: {', '.join(claim.evidence_ids)}")
    if synthesis.limitations:
        lines.append("")
        lines.extend(f"— {limitation}" for limitation in synthesis.limitations)
    return "\n".join(lines)
