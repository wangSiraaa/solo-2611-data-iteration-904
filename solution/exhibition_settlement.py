"""Temporary-exhibition settlement: session selection, audit, frozen contracts.

Model
-----
A venue (one facility) issues **one ticket** covering that facility for one
day.  A ticket holds at most 20 ``Session``s.  Each session fixes a complete,
immovable half-open viewing window ``[start, end)``, two *distinct* contracted
organisations and a whole-session verification payment.  A session pays only
in full, and only when **for its whole window** each of the two organisations
can field one qualified person.  Witnesses may hand over inside a session, but
one person can witness only one session at an instant; adjacent windows may be
switched between directly (``end == next.start`` needs no gap).

Staffing facts come from a :class:`~exhibition_roster.RosterRevision`.  A
person is *usable* for an org at a facility/time only when present there under
exactly that org; if recorded under two orgs with overlapping presence the
person is excluded for both (the dispute rule), so candidate pools of the two
orgs are disjoint at every instant.

The key reduction
-----------------
Take all usable-segment endpoints plus all session-window bounds; they cut the
day into elementary cells.  Within a cell both the candidate pool and the set
of active sessions are constant.  Because a person's capacity resets at every
instant, feasibility decomposes per cell: for each ``(cell, org)`` the number
of selected sessions needing that org must not exceed the number of usable
persons for that org in that cell.  The optimisation is therefore a maximum
weight subset of sessions under these per-cell capacities (a weighted interval
selection with resource capacities), solved exactly by branch and bound
(n <= 20).

The winning plan is rendered per session and per contiguous segment with the
actual person and the roster shift that justifies presence.  Signing freezes
the catalog revision, roster revision and allocation.  After a roster
revision/revocation the ticket can be re-planned, and the old contract's
specific witnesses can be re-checked to list exactly which are now invalid.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Optional, Sequence

from chainage_catalog import Interval, as_fraction, fraction_to_db
from exhibition_roster import RosterBook, RosterRevision, RosterError

MAX_SESSIONS = 20

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SettlementError(ValueError):
    """Base error for exhibition settlement."""


class TicketError(SettlementError):
    """A ticket or session definition is invalid."""


# ---------------------------------------------------------------------------
# Session / ticket definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    session_id: str
    facility_id: str
    window_start: Fraction
    window_end: Fraction
    org_a: str
    org_b: str
    value: Fraction

    def __post_init__(self) -> None:
        if not self.window_start < self.window_end:
            raise TicketError(
                f"session {self.session_id!r} window "
                f"[{self.window_start}, {self.window_end}) is not positive"
            )
        if self.org_a == self.org_b:
            raise TicketError(
                f"session {self.session_id!r} needs two distinct orgs, "
                f"both are {self.org_a!r}"
            )
        if self.value < 0:
            raise TicketError(
                f"session {self.session_id!r} has negative payment {self.value}"
            )

    @property
    def orgs(self) -> tuple[str, str]:
        return (self.org_a, self.org_b)

    @property
    def window(self) -> Interval:
        return Interval(self.window_start, self.window_end)

    def covers(self, t: Fraction) -> bool:
        return self.window_start <= t < self.window_end

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "facility_id": self.facility_id,
            "window": [fraction_to_db(self.window_start), fraction_to_db(self.window_end)],
            "orgs": [self.org_a, self.org_b],
            "value": fraction_to_db(self.value),
        }


def make_session(
    session_id: str,
    facility_id: str,
    window_start,
    window_end,
    org_a: str,
    org_b: str,
    value,
) -> Session:
    return Session(
        session_id=str(session_id),
        facility_id=str(facility_id),
        window_start=as_fraction(window_start),
        window_end=as_fraction(window_end),
        org_a=str(org_a),
        org_b=str(org_b),
        value=as_fraction(value),
    )


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    facility_id: str
    sessions: tuple[Session, ...]

    def __post_init__(self) -> None:
        if len(self.sessions) > MAX_SESSIONS:
            raise TicketError(
                f"ticket {self.ticket_id!r} holds {len(self.sessions)} sessions, "
                f"at most {MAX_SESSIONS} are allowed"
            )
        seen = set()
        for s in self.sessions:
            if s.session_id in seen:
                raise TicketError(
                    f"ticket {self.ticket_id!r} repeats session id {s.session_id!r}"
                )
            seen.add(s.session_id)
            if s.facility_id != self.facility_id:
                raise TicketError(
                    f"session {s.session_id!r} is at facility {s.facility_id!r}, "
                    f"ticket is for {self.facility_id!r}"
                )


def make_ticket(ticket_id: str, facility_id: str, sessions: Iterable[Session]) -> Ticket:
    return Ticket(str(ticket_id), str(facility_id), tuple(sessions))


# ---------------------------------------------------------------------------
# Usable-segment computation (applies the dispute rule)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsableSegment:
    org_id: str
    person_id: str
    start: Fraction
    end: Fraction
    shift_id: str

    @property
    def interval(self) -> Interval:
        return Interval(self.start, self.end)


def facility_usable_segments(
    revision: RosterRevision, facility_id: str
) -> list[UsableSegment]:
    """Split presence at one facility into maximal usable, single-org runs.

    A person present under two orgs at the same instant is disputed and yields
    no usable segment over that overlap (for either org).
    """
    shifts = [s for s in revision.shifts if s.facility_id == facility_id]
    by_person: dict[str, list] = {}
    for s in shifts:
        by_person.setdefault(s.person_id, []).append(s)

    out: list[UsableSegment] = []
    for person_id in sorted(by_person):
        recs = sorted(by_person[person_id], key=lambda s: (s.start, s.end, s.shift_id))
        points = sorted({s.start for s in recs} | {s.end for s in recs})
        for a, b in zip(points, points[1:]):
            if a >= b:
                continue
            active = [s for s in recs if s.start <= a < s.end]
            orgs = sorted({s.org_id for s in active})
            if len(orgs) != 1:
                # 0 = nobody present; >=2 = cross-org dispute -> unusable.
                continue
            org = orgs[0]
            prov = min(s.shift_id for s in active if s.org_id == org)
            out.append(UsableSegment(org, person_id, a, b, prov))
    out.sort(key=lambda u: (u.start, u.org_id, u.person_id, u.shift_id))
    return out


class _Capacity:
    """Per-cell, per-org capacity and provenance built from usable segments."""

    def __init__(self, usable: Sequence[UsableSegment], cells: Sequence[tuple]):
        self.cells = list(cells)
        # cand[org][cell_index] -> {person_id: shift_id}
        self.cand: dict[str, list[dict[str, str]]] = {}
        for org in sorted({u.org_id for u in usable}):
            self.cand[org] = [dict() for _ in self.cells]
        for ci, (a, b) in enumerate(self.cells):
            for u in usable:
                if u.start <= a < u.end:
                    self.cand[u.org_id][ci].setdefault(u.person_id, u.shift_id)

    def capacity(self, org: str, cell_index: int) -> int:
        return len(self.cand.get(org, [{}] * len(self.cells))[cell_index])

    def persons(self, org: str, cell_index: int) -> list[str]:
        d = self.cand.get(org, [{}] * len(self.cells))[cell_index]
        return sorted(d)

    def provenance(self, org: str, cell_index: int, person_id: str) -> Optional[str]:
        return self.cand.get(org, [{}] * len(self.cells))[cell_index].get(person_id)


def _ticket_cells(
    ticket: Ticket, usable: Sequence[UsableSegment]
) -> list[tuple[Fraction, Fraction]]:
    points = set()
    for s in ticket.sessions:
        points.add(s.window_start)
        points.add(s.window_end)
    for u in usable:
        points.add(u.start)
        points.add(u.end)
    ordered = sorted(points)
    return [(a, b) for a, b in zip(ordered, ordered[1:]) if a < b]


def _cells_in_window(cells, start, end) -> list[int]:
    return [
        i for i, (a, _b) in enumerate(cells) if start <= a < end
    ]


# ---------------------------------------------------------------------------
# Plan result value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WitnessSegment:
    session_id: str
    org_id: str
    person_id: str
    shift_id: str
    start: Fraction
    end: Fraction
    roster_book_id: str
    roster_revision_id: str
    catalog_revision: str

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "org_id": self.org_id,
            "person_id": self.person_id,
            "shift_id": self.shift_id,
            "start": fraction_to_db(self.start),
            "end": fraction_to_db(self.end),
            "roster_book_id": self.roster_book_id,
            "roster_revision_id": self.roster_revision_id,
            "catalog_revision": self.catalog_revision,
        }


@dataclass(frozen=True)
class PlannedSession:
    session: Session
    segments: tuple[WitnessSegment, ...]

    def to_dict(self) -> dict:
        return {
            "session": self.session.to_dict(),
            "segments": [s.to_dict() for s in self.segments],
        }


@dataclass(frozen=True)
class PlanResult:
    ticket_id: str
    facility_id: str
    roster_book_id: str
    roster_revision_id: str
    catalog_revision: str
    total_value: Fraction
    selected: tuple[PlannedSession, ...]
    signable: tuple[str, ...]
    unsigned: tuple[str, ...]

    def selected_ids(self) -> list[str]:
        return [p.session.session_id for p in self.selected]

    def witnesses(self) -> list[WitnessSegment]:
        return [seg for p in self.selected for seg in p.segments]

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "facility_id": self.facility_id,
            "roster_book_id": self.roster_book_id,
            "roster_revision_id": self.roster_revision_id,
            "catalog_revision": self.catalog_revision,
            "total_value": fraction_to_db(self.total_value),
            "selected": [p.to_dict() for p in self.selected],
            "signable": list(self.signable),
            "unsigned": list(self.unsigned),
        }


# ---------------------------------------------------------------------------
# Planner (exact branch and bound)
# ---------------------------------------------------------------------------


class SettlementPlanner:
    """Selects an optimal signed subset and renders an auditable plan."""

    def __init__(self, book: RosterBook):
        self.book = book

    def plan(
        self,
        ticket: Ticket,
        roster_revision_id: Optional[str] = None,
    ) -> PlanResult:
        if roster_revision_id is None:
            revision = self.book.current()
        else:
            revision = self.book.get(roster_revision_id)

        usable = facility_usable_segments(revision, ticket.facility_id)
        cells = _ticket_cells(ticket, usable)
        cap = _Capacity(usable, cells)

        sessions = list(ticket.sessions)
        # Individual signability: both orgs staffed across the whole window.
        signable: list[Session] = []
        signable_ids: list[str] = []
        unsigned_ids: list[str] = []
        demands: dict[str, list[tuple[str, int]]] = {}
        for s in sessions:
            cell_ids = _cells_in_window(cells, s.window_start, s.window_end)
            ok = bool(cell_ids)
            for ci in cell_ids:
                for org in s.orgs:
                    if cap.capacity(org, ci) < 1:
                        ok = False
            if ok:
                signable.append(s)
                signable_ids.append(s.session_id)
                req = []
                for ci in cell_ids:
                    req.append((s.org_a, ci))
                    req.append((s.org_b, ci))
                demands[s.session_id] = req
            else:
                unsigned_ids.append(s.session_id)

        selected_idx = self._optimise(signable, demands, cap)

        # -- render the assignment deterministically ----------------------
        selected_sessions = [signable[i] for i in sorted(selected_idx)]
        # active[(cell, org)] -> list of session ids needing it, sorted.
        active: dict[tuple[int, str], list[str]] = {}
        for s in selected_sessions:
            for ci in _cells_in_window(cells, s.window_start, s.window_end):
                for org in s.orgs:
                    active.setdefault((ci, org), []).append(s.session_id)
        for key in active:
            active[key].sort()

        # assignment[(session_id, org, cell_index)] -> (person, shift)
        assignment: dict[tuple[str, str, int], tuple[str, str]] = {}
        for (ci, org), sids in active.items():
            persons = cap.persons(org, ci)
            if len(persons) < len(sids):
                # Should not happen for a feasible subset; defensive guard.
                raise SettlementError(
                    f"internal inconsistency: cell {ci} org {org} needs "
                    f"{len(sids)} witnesses, only {len(persons)} usable"
                )
            for sid, person in zip(sids, persons):
                shift = cap.provenance(org, ci, person)
                assignment[(sid, org, ci)] = (person, shift)

        planned: list[PlannedSession] = []
        for s in selected_sessions:
            segs = self._merge_segments(
                s, cells, assignment, revision, ticket.facility_id
            )
            planned.append(PlannedSession(s, tuple(segs)))

        total = sum((s.value for s in selected_sessions), Fraction(0))
        return PlanResult(
            ticket_id=ticket.ticket_id,
            facility_id=ticket.facility_id,
            roster_book_id=self.book.book_id,
            roster_revision_id=revision.revision_id,
            catalog_revision=revision.catalog_revision,
            total_value=total,
            selected=tuple(planned),
            signable=tuple(sorted(signable_ids)),
            unsigned=tuple(sorted(unsigned_ids)),
        )

    # -- optimisation -------------------------------------------------
    def _optimise(self, sessions, demands, cap) -> set[int]:
        """Exact max-value feasible subset (branch and bound on n <= 20)."""
        n = len(sessions)
        if n == 0:
            return set()
        order = sorted(
            range(n),
            key=lambda i: (-sessions[i].value, sessions[i].session_id),
        )
        vals = [sessions[i].value for i in order]
        suffix = [Fraction(0)] * (n + 1)
        for k in range(n - 1, -1, -1):
            suffix[k] = suffix[k + 1] + vals[k]
        keys = [demands[sessions[i].session_id] for i in order]

        counts: dict[tuple[int, str], int] = {}
        best_value = Fraction(0)
        best_set: set[int] = set()

        def feasible_add(k: int) -> bool:
            for org, ci in keys[k]:
                if counts.get((ci, org), 0) + 1 > cap.capacity(org, ci):
                    return False
            return True

        def add(k: int) -> None:
            for org, ci in keys[k]:
                counts[(ci, org)] = counts.get((ci, org), 0) + 1

        def remove(k: int) -> None:
            for org, ci in keys[k]:
                key = (ci, org)
                counts[key] -= 1
                if counts[key] == 0:
                    del counts[key]

        def dfs(k: int, value: Fraction, chosen: list[int]) -> None:
            nonlocal best_value, best_set
            if value + suffix[k] <= best_value:
                return
            if k == n:
                best_value = value
                best_set = set(order[i] for i in chosen)
                return
            if feasible_add(k):
                add(k)
                chosen.append(k)
                dfs(k + 1, value + vals[k], chosen)
                chosen.pop()
                remove(k)
            dfs(k + 1, value, chosen)

        # Greedy seed: adopt every session (in descending value) that still
        # fits.  Gives the branch-and-bound a non-trivial incumbent early,
        # so the suffix bound prunes ties instead of enumerating them.
        greedy: list[int] = []
        for k in range(n):
            if feasible_add(k):
                add(k)
                greedy.append(k)
        for k in reversed(greedy):
            remove(k)
        greedy_set = set(order[k] for k in greedy)
        greedy_value = sum((vals[k] for k in greedy), Fraction(0))
        if greedy_value > best_value:
            best_value = greedy_value
            best_set = greedy_set

        dfs(0, Fraction(0), [])
        return best_set

    # -- rendering ----------------------------------------------------
    def _merge_segments(
        self, session, cells, assignment, revision, facility_id
    ) -> list[WitnessSegment]:
        segs: list[WitnessSegment] = []
        cell_ids = _cells_in_window(cells, session.window_start, session.window_end)
        for org in session.orgs:
            run = None  # (person, shift, start, end)
            for ci in cell_ids:
                person, shift = assignment[(session.session_id, org, ci)]
                a, b = cells[ci]
                if run and run[0] == person and run[1] == shift and run[3] == a:
                    run = (person, shift, run[2], b)
                else:
                    if run is not None:
                        segs.append(self._mk(session, org, run, revision))
                    run = (person, shift, a, b)
            if run is not None:
                segs.append(self._mk(session, org, run, revision))
        segs.sort(key=lambda s: (s.start, s.org_id, s.person_id))
        return segs

    @staticmethod
    def _mk(session, org, run, revision) -> WitnessSegment:
        person, shift, a, b = run
        return WitnessSegment(
            session_id=session.session_id,
            org_id=org,
            person_id=person,
            shift_id=shift,
            start=a,
            end=b,
            roster_book_id=revision.book_id,
            roster_revision_id=revision.revision_id,
            catalog_revision=revision.catalog_revision,
        )


# ---------------------------------------------------------------------------
# Frozen contract and post-hoc invalidation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Contract:
    contract_id: str
    ticket_id: str
    facility_id: str
    roster_book_id: str
    roster_revision_id: str
    catalog_revision: str
    total_value: Fraction
    sessions: tuple[PlannedSession, ...]

    def to_dict(self) -> dict:
        return {
            "contract_id": self.contract_id,
            "ticket_id": self.ticket_id,
            "facility_id": self.facility_id,
            "roster_book_id": self.roster_book_id,
            "roster_revision_id": self.roster_revision_id,
            "catalog_revision": self.catalog_revision,
            "total_value": fraction_to_db(self.total_value),
            "sessions": [p.to_dict() for p in self.sessions],
        }

    def fingerprint(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sign_contract(contract_id: str, plan: PlanResult) -> Contract:
    """Freeze a plan's catalog revision, roster revision and allocation."""
    return Contract(
        contract_id=str(contract_id),
        ticket_id=plan.ticket_id,
        facility_id=plan.facility_id,
        roster_book_id=plan.roster_book_id,
        roster_revision_id=plan.roster_revision_id,
        catalog_revision=plan.catalog_revision,
        total_value=plan.total_value,
        sessions=plan.selected,
    )


@dataclass(frozen=True)
class InvalidWitness:
    segment: WitnessSegment
    reason: str

    def to_dict(self) -> dict:
        return {**self.segment.to_dict(), "reason": self.reason}


def _overlapping_shifts(
    revision: RosterRevision, facility_id: str, person_id: str,
    start: Fraction, end: Fraction,
) -> list:
    """The person's raw shifts at the facility that intersect ``[start, end)``."""
    return [
        s
        for s in revision.shifts
        if s.facility_id == facility_id
        and s.person_id == person_id
        and s.start < end
        and start < s.end
    ]


def _reason_for_segment(
    revision: RosterRevision, facility_id: str, seg: WitnessSegment
) -> Optional[str]:
    """Why a frozen witness can no longer be justified, or None if it holds."""
    # 1. Is there a *usable* run covering the whole segment for this org?
    for u in facility_usable_segments(revision, facility_id):
        if (
            u.org_id == seg.org_id
            and u.person_id == seg.person_id
            and u.start <= seg.start
            and seg.end <= u.end
        ):
            return None
    # 2. Raw presence overlapping the segment.
    overlapping = _overlapping_shifts(
        revision, facility_id, seg.person_id, seg.start, seg.end
    )
    if not overlapping:
        return "person_not_present"
    # 3. Any instant inside the segment where the person is under two orgs?
    points = {seg.start, seg.end}
    for s in overlapping:
        points.add(max(s.start, seg.start))
        points.add(min(s.end, seg.end))
    for t in sorted(points):
        if not (seg.start <= t < seg.end):
            continue
        orgs = {s.org_id for s in overlapping if s.start <= t < s.end}
        if len(orgs) >= 2:
            return "cross_org_dispute"
    # 4. Present for only part of the segment, under no dispute.
    covered = _union_length(overlapping, seg.start, seg.end)
    if covered < (seg.end - seg.start):
        return "presence_gap"
    # 5. Present throughout, but under the wrong org only.
    return "not_usable_for_org"


def _union_length(shifts, start: Fraction, end: Fraction) -> Fraction:
    """Measure of the union of ``shifts`` clipped to ``[start, end)``."""
    clipped = [
        (max(s.start, start), min(s.end, end))
        for s in shifts
    ]
    clipped = sorted(a for a in clipped if a[0] < a[1])
    total = Fraction(0)
    cur_lo = cur_hi = None
    for a, b in clipped:
        if cur_hi is None or a > cur_hi:
            if cur_hi is not None:
                total += cur_hi - cur_lo
            cur_lo, cur_hi = a, b
        elif b > cur_hi:
            cur_hi = b
    if cur_hi is not None:
        total += cur_hi - cur_lo
    return total


def invalid_witnesses(
    contract: Contract,
    current_revision: RosterRevision,
) -> list[InvalidWitness]:
    """Re-check every frozen witness against ``current_revision``.

    Returns the segments that can no longer be justified, each with a reason.
    If the current revision is a revocation, every witness is invalid.
    """
    bad: list[InvalidWitness] = []
    for planned in contract.sessions:
        for seg in planned.segments:
            if current_revision.revoked:
                bad.append(InvalidWitness(seg, "roster_revoked"))
                continue
            reason = _reason_for_segment(
                current_revision, contract.facility_id, seg
            )
            if reason is not None:
                bad.append(InvalidWitness(seg, reason))
    return bad


# ---------------------------------------------------------------------------
# SQLite persistence (settlement facts, separate schema)
# ---------------------------------------------------------------------------


SETTLEMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS contract (
    contract_id       TEXT PRIMARY KEY,
    ticket_id         TEXT NOT NULL,
    facility_id       TEXT NOT NULL,
    roster_book_id    TEXT NOT NULL,
    roster_revision_id TEXT NOT NULL,
    catalog_revision  TEXT NOT NULL,
    total_num         TEXT NOT NULL,
    total_den         TEXT NOT NULL,
    fingerprint       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contract_session (
    contract_id TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    window_start_num TEXT NOT NULL, window_start_den TEXT NOT NULL,
    window_end_num   TEXT NOT NULL, window_end_den   TEXT NOT NULL,
    org_a       TEXT NOT NULL,
    org_b       TEXT NOT NULL,
    value_num   TEXT NOT NULL, value_den TEXT NOT NULL,
    PRIMARY KEY (contract_id, session_id)
);
CREATE TABLE IF NOT EXISTS contract_segment (
    contract_id TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    seg_idx     INTEGER NOT NULL,
    org_id      TEXT NOT NULL,
    person_id   TEXT NOT NULL,
    shift_id    TEXT NOT NULL,
    start_num   TEXT NOT NULL, start_den TEXT NOT NULL,
    end_num     TEXT NOT NULL, end_den   TEXT NOT NULL,
    roster_book_id     TEXT NOT NULL,
    roster_revision_id TEXT NOT NULL,
    catalog_revision   TEXT NOT NULL,
    PRIMARY KEY (contract_id, session_id, seg_idx)
);
"""


def ensure_settlement_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SETTLEMENT_SCHEMA)


def persist_contract(conn: sqlite3.Connection, contract: Contract) -> None:
    with conn:
        existing = conn.execute(
            "SELECT fingerprint FROM contract WHERE contract_id = ?",
            (contract.contract_id,),
        ).fetchone()
        if existing is not None:
            if existing["fingerprint"] != contract.fingerprint():
                raise SettlementError(
                    f"contract {contract.contract_id!r} stored with different content"
                )
            return
        conn.execute(
            "INSERT INTO contract (contract_id, ticket_id, facility_id, "
            "roster_book_id, roster_revision_id, catalog_revision, total_num, "
            "total_den, fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                contract.contract_id, contract.ticket_id, contract.facility_id,
                contract.roster_book_id, contract.roster_revision_id,
                contract.catalog_revision,
                str(contract.total_value.numerator),
                str(contract.total_value.denominator),
                contract.fingerprint(),
            ),
        )
        for planned in contract.sessions:
            s = planned.session
            conn.execute(
                "INSERT INTO contract_session (contract_id, session_id, "
                "window_start_num, window_start_den, window_end_num, window_end_den, "
                "org_a, org_b, value_num, value_den) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    contract.contract_id, s.session_id,
                    str(s.window_start.numerator), str(s.window_start.denominator),
                    str(s.window_end.numerator), str(s.window_end.denominator),
                    s.org_a, s.org_b,
                    str(s.value.numerator), str(s.value.denominator),
                ),
            )
            for i, seg in enumerate(planned.segments):
                conn.execute(
                    "INSERT INTO contract_segment (contract_id, session_id, seg_idx, "
                    "org_id, person_id, shift_id, start_num, start_den, end_num, "
                    "end_den, roster_book_id, roster_revision_id, catalog_revision) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        contract.contract_id, s.session_id, i, seg.org_id,
                        seg.person_id, seg.shift_id,
                        str(seg.start.numerator), str(seg.start.denominator),
                        str(seg.end.numerator), str(seg.end.denominator),
                        seg.roster_book_id, seg.roster_revision_id,
                        seg.catalog_revision,
                    ),
                )


def load_contract(conn: sqlite3.Connection, contract_id: str) -> Contract:
    row = conn.execute(
        "SELECT * FROM contract WHERE contract_id = ?", (contract_id,)
    ).fetchone()
    if row is None:
        raise SettlementError(f"no contract {contract_id!r}")
    sessions = []
    for srow in conn.execute(
        "SELECT * FROM contract_session WHERE contract_id = ? ORDER BY session_id",
        (contract_id,),
    ).fetchall():
        session = Session(
            session_id=srow["session_id"],
            facility_id=row["facility_id"],
            window_start=Fraction(int(srow["window_start_num"]), int(srow["window_start_den"])),
            window_end=Fraction(int(srow["window_end_num"]), int(srow["window_end_den"])),
            org_a=srow["org_a"],
            org_b=srow["org_b"],
            value=Fraction(int(srow["value_num"]), int(srow["value_den"])),
        )
        segs = [
            WitnessSegment(
                session_id=r["session_id"],
                org_id=r["org_id"],
                person_id=r["person_id"],
                shift_id=r["shift_id"],
                start=Fraction(int(r["start_num"]), int(r["start_den"])),
                end=Fraction(int(r["end_num"]), int(r["end_den"])),
                roster_book_id=r["roster_book_id"],
                roster_revision_id=r["roster_revision_id"],
                catalog_revision=r["catalog_revision"],
            )
            for r in conn.execute(
                "SELECT * FROM contract_segment WHERE contract_id = ? "
                "AND session_id = ? ORDER BY seg_idx",
                (contract_id, srow["session_id"]),
            ).fetchall()
        ]
        sessions.append(PlannedSession(session, tuple(segs)))
    return Contract(
        contract_id=row["contract_id"],
        ticket_id=row["ticket_id"],
        facility_id=row["facility_id"],
        roster_book_id=row["roster_book_id"],
        roster_revision_id=row["roster_revision_id"],
        catalog_revision=row["catalog_revision"],
        total_value=Fraction(int(row["total_num"]), int(row["total_den"])),
        sessions=tuple(sessions),
    )
