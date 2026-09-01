"""The SEC's own item semantics, transcribed and mapped. No inference.

Every classification in this phase comes from this table. An 8-K carrying Item
4.02 establishes a restatement because the item's official title is
*Non-Reliance on Previously Issued Financial Statements* -- the registrant
asserted it by filing under that item, and no reading of the document is
involved. That is what makes the pipeline deterministic, and it is also the
whole of its reach: an item code names a category, never a business outcome.

The line this table does not cross
----------------------------------
Item 1.01 is *Entry into a Material Definitive Agreement*. It establishes that
an agreement was entered into and that the registrant considered it material.
It does not establish a customer win, a supply deal or a partnership, and the
summary written from it says only what the item says. Widening a broad SEC
category into a specific business claim is the failure mode this whole layer is
built to avoid, and it would be invisible in the output -- a confident sentence
about a contract nobody filed.

Administrative items
--------------------
Item 9.01 is *Financial Statements and Exhibits*: it records that exhibits are
attached, which is bookkeeping about the filing rather than an occurrence at
the company. It produces no event. Items 7.01 (Regulation FD) and 8.01 (Other
Events) are real disclosures whose subject is deliberately unconstrained by the
form, so they classify as unclassified rather than being guessed at.
"""

from __future__ import annotations

from typing import Final

from app.research_intelligence.schemas import EventKind, Materiality

ITEM_TITLES: Final[dict[str, str]] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety - Reporting of Shutdowns and Patterns of Violations",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate or Increase a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure of Directors or Certain Officers; Election of Directors",
    "5.03": "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year",
    "5.04": "Temporary Suspension of Trading Under Employee Benefit Plans",
    "5.05": "Amendment to Code of Ethics, or Waiver of a Provision",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}
"""Official 8-K item titles. The wording a summary is built from."""

ADMINISTRATIVE_ITEMS: Final[frozenset[str]] = frozenset({"9.01"})
"""Items describing the filing rather than an occurrence. No event is emitted."""

ITEM_KINDS: Final[dict[str, EventKind]] = {
    "1.01": EventKind.MATERIAL_AGREEMENT,
    "1.02": EventKind.MATERIAL_AGREEMENT,
    "1.03": EventKind.BANKRUPTCY_OR_RECEIVERSHIP,
    "1.05": EventKind.CYBERSECURITY_INCIDENT,
    "2.01": EventKind.M_AND_A,
    "2.02": EventKind.EARNINGS_RELEASE,
    "2.03": EventKind.DEBT_EVENT,
    "2.04": EventKind.DEBT_EVENT,
    "2.05": EventKind.EXIT_OR_DISPOSAL_COSTS,
    "2.06": EventKind.IMPAIRMENT,
    "3.01": EventKind.LISTING_RULE_MATTER,
    "3.02": EventKind.UNREGISTERED_EQUITY_SALE,
    "4.01": EventKind.AUDITOR_CHANGE,
    "4.02": EventKind.ACCOUNTING_RESTATEMENT,
    "5.01": EventKind.CONTROL_CHANGE,
    "5.02": EventKind.MANAGEMENT_CHANGE,
}
"""Item code to event kind, where the item's own title names the event.

Absent on purpose: 1.04 (mine safety), 3.03, 5.03 through 5.08, 7.01 and 8.01.
Each is either narrow to an industry, procedural, or -- for 7.01 and 8.01 --
deliberately open-ended, so the form establishes that *something* was disclosed
without establishing what. Those filings are recorded as
``UNCLASSIFIED_SEC_FILING`` and remain visible."""

FORM_KINDS: Final[dict[str, EventKind]] = {
    "10-K": EventKind.PERIODIC_REPORT,
    "10-Q": EventKind.PERIODIC_REPORT,
    "20-F": EventKind.PERIODIC_REPORT,
    "40-F": EventKind.PERIODIC_REPORT,
}
"""Forms whose own identity establishes the event, without item codes.

A 10-Q is the quarterly report; saying so claims nothing the form does not.
``6-K`` is deliberately absent: it is the catch-all *Report of Foreign Private
Issuer*, and its contents are unconstrained by the form -- one may carry
earnings, another a press release, another a change of auditor. That is the
coverage boundary, and it stays visible rather than being papered over with a
kind that sounds informative."""

FORM_MATERIALITY: Final[dict[str, Materiality]] = {
    "10-K": Materiality.SIGNIFICANT,
    "20-F": Materiality.SIGNIFICANT,
    "40-F": Materiality.SIGNIFICANT,
    "10-Q": Materiality.NOTABLE,
}
"""Annual reports outrank quarterly ones -- the same split
``app.monitoring.materiality`` already draws between its ``MATERIAL_FORMS``
and ``NOTABLE_FORMS``. One vocabulary across the system."""

KIND_MATERIALITY: Final[dict[EventKind, Materiality]] = {
    EventKind.ACCOUNTING_RESTATEMENT: Materiality.CRITICAL,
    EventKind.BANKRUPTCY_OR_RECEIVERSHIP: Materiality.CRITICAL,
    EventKind.CONTROL_CHANGE: Materiality.CRITICAL,
    EventKind.M_AND_A: Materiality.SIGNIFICANT,
    EventKind.EARNINGS_RELEASE: Materiality.SIGNIFICANT,
    EventKind.AUDITOR_CHANGE: Materiality.SIGNIFICANT,
    EventKind.IMPAIRMENT: Materiality.SIGNIFICANT,
    EventKind.LISTING_RULE_MATTER: Materiality.SIGNIFICANT,
    EventKind.CYBERSECURITY_INCIDENT: Materiality.SIGNIFICANT,
    EventKind.MANAGEMENT_CHANGE: Materiality.NOTABLE,
    EventKind.MATERIAL_AGREEMENT: Materiality.NOTABLE,
    EventKind.DEBT_EVENT: Materiality.NOTABLE,
    EventKind.EXIT_OR_DISPOSAL_COSTS: Materiality.NOTABLE,
    EventKind.UNREGISTERED_EQUITY_SALE: Materiality.NOTABLE,
    EventKind.PERIODIC_REPORT: Materiality.NOTABLE,
    EventKind.UNCLASSIFIED_SEC_FILING: Materiality.ROUTINE,
}
"""Attention, derived from form semantics. **Not direction, and not tuned.**

These bands come from what the item asserts, never from what prices did
afterwards -- no threshold here has been fitted against an outcome, and
``docs/filing-events.md`` records why that would be futile anyway: post-filing
windows lifted regardless of EPS direction, and the effect did not survive
pre-registration.

The two ends illustrate the rule. ``ACCOUNTING_RESTATEMENT`` is CRITICAL
because the registrant has stated that previously issued financials cannot be
relied upon -- every other figure Tradabot holds for that company is now in
question, which is a fact about data quality rather than a view on the shares.
``MANAGEMENT_CHANGE`` is only NOTABLE because Item 5.02 covers a chief
executive resigning and a director being elected at the annual meeting with
equal standing, and metadata cannot separate them. Rating it higher would mean
rating routine board administration as significant."""

_MULTI_ITEM_RATIONALE = """One filing, one event per meaningful item.

An 8-K carrying Items 1.01 and 5.02 reports two unrelated occurrences: a
material agreement and a change of officers. They differ in materiality, they
age differently, and a reader asking "what management changes happened this
quarter" should find the second without the first. Collapsing them into one
event with a list of classifications would force a single materiality band and
a single freshness window onto two different things, and would make
``events_by_kind`` unable to answer its own question.

So the filing produces N events sharing an accession, each with its own
``classifying_item`` and each carrying the full ``item_codes`` tuple so the
grouping is never lost. Idempotency is preserved because the event identity
includes the item: re-ingesting the filing regenerates the same N identities.
"""


def kind_for(item: str) -> EventKind | None:
    """The event an item code establishes, or ``None`` if it establishes none."""
    return ITEM_KINDS.get(item.strip())


def title_for(item: str) -> str | None:
    return ITEM_TITLES.get(item.strip())


def kind_for_form(form: str) -> EventKind | None:
    """The event a form establishes on its own, or ``None``."""
    return FORM_KINDS.get(form.strip())


def materiality_for(kind: EventKind, form: str = "") -> Materiality:
    """Attention band. ``form`` refines it only for periodic reports, where an
    annual report outranks a quarterly one."""
    if kind is EventKind.PERIODIC_REPORT:
        return FORM_MATERIALITY.get(form.strip(), Materiality.NOTABLE)
    return KIND_MATERIALITY.get(kind, Materiality.ROUTINE)


def is_administrative(item: str) -> bool:
    return item.strip() in ADMINISTRATIVE_ITEMS


def summarise(kind: EventKind, item: str | None, form: str) -> str:
    """A sentence that says only what the form and item say.

    Built from the SEC's own item title rather than written per event kind, so
    the summary cannot drift away from what the source asserts. It names the
    item so a reader can check the claim against the same table the classifier
    used.
    """
    if kind is EventKind.UNCLASSIFIED_SEC_FILING:
        return (
            f"SEC filing of form {form} recorded. Its item metadata does not "
            f"establish a specific event kind."
        )
    if kind is EventKind.PERIODIC_REPORT:
        return f"SEC periodic report of form {form} filed."
    title = title_for(item or "")
    if title is None:
        return f"SEC filing of form {form} reports an event under item {item}."
    return f"SEC filing of form {form} reports an event under Item {item}, {title}."
