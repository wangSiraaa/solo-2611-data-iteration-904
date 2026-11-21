"""Temporary-exhibition rosters ("班簿"): revisions, attribution and lookup.

A roster revision is a **full snapshot** resolved against a named catalogue
revision.  It uses a named section plus half-open label ranges to identify
which facilities the roster actually covers, and records the people,
organisations and half-open presence times at those facilities.  A later
revision may *replace* the snapshot wholesale or *revoke* it; every revision
stays queryable as its own on-site snapshot ("截面").

Attribution rule (half-open time everywhere)
--------------------------------------------
If the *same person* is recorded by two different organisations at the *same
facility* with overlapping presence, then in the overlap neither organisation
may use that person.  Everyone else, and every other interval, remains usable.
Adjacent presence intervals may hand over to a different person at the
boundary (``end == next.start``).

Facilities are fixed physical identities (see :mod:`inspection_library`), so
two sections that carry the *same* chainage label still map to different
facilities.  Moving a person's presence to "another section with the same
label" therefore moves them to a different facility for the same times.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Optional, Sequence

from chainage_catalog import (
    CatalogLookupError,
    CatalogRegistry,
    Interval,
    as_fraction,
    fraction_to_db,
    merge_intervals,
)
from inspection_library import (
    Asset,
    AssetsIndex,
    covered_by,
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RosterError(ValueError):
    """Base error for roster processing."""


class RosterRevisionError(RosterError):
    """A roster revision failed validation."""


class RosterQueryError(RosterError):
    """A lookup referenced an unknown book / revision / facility."""


# ---------------------------------------------------------------------------
# Presence and span value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Shift:
    """One presence record: a person on duty for one org at one facility."""

    shift_id: str
    facility_id: str
    person_id: str
    org_id: str
    start: Fraction
    end: Fraction

    def __post_init__(self) -> None:
        if not self.start < self.end:
            raise RosterError(
                f"shift {self.shift_id!r} has non-positive presence interval "
                f"[{self.start}, {self.end})"
            )

    def covers(self, t: Fraction) -> bool:
        """Half-open membership: left end included, right end excluded."""
        return self.start <= t < self.end

    @property
    def length(self) -> Fraction:
        return self.end - self.start

    def to_canonical(self) -> dict:
        return {
            "shift_id": self.shift_id,
            "facility_id": self.facility_id,
            "person_id": self.person_id,
            "org_id": self.org_id,
            "start": fraction_to_db(self.start),
            "end": fraction_to_db(self.end),
        }


def make_shift(
    shift_id: str,
    facility_id: str,
    person_id: str,
    org_id: str,
    start,
    end,
) -> Shift:
    """Build a validated :class:`Shift` from raw (int/float/str) times."""
    return Shift(
        shift_id=str(shift_id),
        facility_id=str(facility_id),
        person_id=str(person_id),
        org_id=str(org_id),
        start=as_fraction(start),
        end=as_fraction(end),
    )


@dataclass(frozen=True)
class RosterSpan:
    """A resolved label span: names the section and the physical extent."""

    section_id: str
    label_start: Fraction
    label_end: Fraction
    physical_start: Fraction
    physical_end: Fraction

    @property
    def physical(self) -> Interval:
        return Interval(self.physical_start, self.physical_end)

    def to_canonical(self) -> dict:
        return {
            "section_id": self.section_id,
            "label_start": fraction_to_db(self.label_start),
            "label_end": fraction_to_db(self.label_end),
            "physical_start": fraction_to_db(self.physical_start),
            "physical_end": fraction_to_db(self.physical_end),
        }


# ---------------------------------------------------------------------------
# Roster revision (immutable full snapshot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RosterRevision:
    revision_id: str
    book_id: str
    kind: str  # "replace" | "revoke"
    catalog_revision: str
    route_id: str
    spans: tuple[RosterSpan, ...]
    covered_facilities: tuple[str, ...]
    shifts: tuple[Shift, ...]

    @property
    def revoked(self) -> bool:
        return self.kind == "revoke"

    def facility_shifts(self, facility_id: str) -> tuple[Shift, ...]:
        return tuple(s for s in self.shifts if s.facility_id == facility_id)

    def to_canonical(self) -> dict:
        return {
            "revision_id": self.revision_id,
            "book_id": self.book_id,
            "kind": self.kind,
            "catalog_revision": self.catalog_revision,
            "route_id": self.route_id,
            "spans": [s.to_canonical() for s in self.spans],
            "covered_facilities": list(self.covered_facilities),
            "shifts": [s.to_canonical() for s in self.shifts],
        }

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_canonical(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DisputePerson:
    """A person present at one facility under two or more organisations."""

    person_id: str
    orgs: tuple[str, ...]
    shift_ids: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "person_id": self.person_id,
            "orgs": list(self.orgs),
            "shift_ids": list(self.shift_ids),
        }


@dataclass(frozen=True)
class TimelineSegment:
    """One elementary half-open time segment of a facility's staffed day."""

    start: Fraction
    end: Fraction
    staff: dict[str, tuple[str, ...]]  # org -> present (usable) persons
    disputed: tuple[DisputePerson, ...]

    def to_dict(self) -> dict:
        return {
            "start": fraction_to_db(self.start),
            "end": fraction_to_db(self.end),
            "staff": {org: list(people) for org, people in self.staff.items()},
            "disputed": [d.to_dict() for d in self.disputed],
        }


# ---------------------------------------------------------------------------
# Attribution helpers
# ---------------------------------------------------------------------------


def classify_at(
    shifts: Sequence[Shift], t
) -> tuple[dict[str, tuple[str, ...]], tuple[DisputePerson, ...]]:
    """Classify one instant.

    Returns ``(staff, disputes)`` where ``staff`` maps an org to the tuple of
    *usable* persons and ``disputes`` lists persons present under two or more
    orgs (excluded from every org for the overlapping instant).
    """
    t = as_fraction(t)
    active = [s for s in shifts if s.covers(t)]
    by_person: dict[str, list[Shift]] = {}
    for s in active:
        by_person.setdefault(s.person_id, []).append(s)

    staff: dict[str, list[str]] = {}
    disputes: list[DisputePerson] = []
    for person_id in sorted(by_person):
        recs = by_person[person_id]
        orgs = sorted({r.org_id for r in recs})
        if len(orgs) >= 2:
            disputes.append(
                DisputePerson(
                    person_id=person_id,
                    orgs=tuple(orgs),
                    shift_ids=tuple(sorted({r.shift_id for r in recs})),
                )
            )
        else:
            staff.setdefault(orgs[0], []).append(person_id)
    return (
        {org: tuple(sorted(people)) for org, people in sorted(staff.items())},
        tuple(disputes),
    )


def usable_persons(shifts: Sequence[Shift], t) -> dict[str, tuple[str, ...]]:
    """Usable persons per org at instant ``t`` (disputes already removed)."""
    staff, _ = classify_at(shifts, t)
    return staff


def shift_for(
    shifts: Sequence[Shift],
    facility_id: str,
    person_id: str,
    org_id: str,
    start: Fraction,
    end: Fraction,
) -> Optional[str]:
    """The deterministic shift id justifying presence on ``[start, end)``."""
    covering = [
        s
        for s in shifts
        if s.facility_id == facility_id
        and s.person_id == person_id
        and s.org_id == org_id
        and s.start <= start
        and end <= s.end
    ]
    if not covering:
        return None
    return min(s.shift_id for s in covering)


def facility_timeline(
    revision: RosterRevision,
    facility_id: str,
    window_start=None,
    window_end=None,
) -> list[TimelineSegment]:
    """Break a facility's day into elementary segments with staffing facts.

    Every shift boundary (and optional window bound) is a breakpoint, so
    presence and disputes are constant within a segment.  Gaps where nobody is
    present are reported too, with an empty staff map.
    """
    shifts = revision.facility_shifts(facility_id)
    if not shifts:
        return []
    points = {s.start for s in shifts} | {s.end for s in shifts}
    if window_start is not None:
        points.add(as_fraction(window_start))
    if window_end is not None:
        points.add(as_fraction(window_end))
    ordered = sorted(points)
    segments: list[TimelineSegment] = []
    for a, b in zip(ordered, ordered[1:]):
        if a >= b:
            continue
        if window_start is not None and b <= as_fraction(window_start):
            continue
        if window_end is not None and a >= as_fraction(window_end):
            continue
        staff, disputes = classify_at(shifts, a)
        segments.append(TimelineSegment(a, b, staff, disputes))
    return segments


# ---------------------------------------------------------------------------
# Roster book: ordered revisions with replace / revoke
# ---------------------------------------------------------------------------


class RosterBook:
    """Ordered revisions of one logical roster; newest is ``current()``."""

    def __init__(
        self,
        book_id: str,
        route_id: str,
        registry: CatalogRegistry,
        assets: AssetsIndex,
    ):
        self.book_id = str(book_id)
        self.route_id = str(route_id)
        self.registry = registry
        self.assets = assets
        self._revisions: list[RosterRevision] = []
        self._by_id: dict[str, RosterRevision] = {}

    # -- construction -------------------------------------------------
    def _insert(self, revision: RosterRevision) -> None:
        if revision.revision_id in self._by_id:
            raise RosterRevisionError(
                f"revision {revision.revision_id!r} already exists in book "
                f"{self.book_id!r}"
            )
        self._revisions.append(revision)
        self._by_id[revision.revision_id] = revision

    def add_replace(
        self,
        revision_id: str,
        catalog_revision: str,
        spans: Iterable[dict],
        shifts: Iterable[dict | Shift],
    ) -> RosterRevision:
        """Add a full replacement snapshot.

        ``spans`` are ``{section_id, label_start, label_end}`` entries resolved
        against ``catalog_revision``.  Their union identifies the covered
        facilities.  Every shift must be posted at a covered facility.
        """
        try:
            catalog = self.registry.get(catalog_revision)
        except CatalogLookupError as exc:
            raise RosterRevisionError(
                f"revision {revision_id!r} names unknown catalogue revision "
                f"{catalog_revision!r}"
            ) from exc
        if catalog.route_id != self.route_id:
            raise RosterRevisionError(
                f"catalogue revision {catalog_revision!r} is route "
                f"{catalog.route_id!r}, book is route {self.route_id!r}"
            )
        resolved: list[RosterSpan] = []
        intervals: list[Interval] = []
        spans = list(spans)
        if not spans:
            raise RosterRevisionError(
                f"replacement revision {revision_id!r} needs at least one span"
            )
        for i, span in enumerate(spans):
            try:
                c = catalog.convert_span(
                    str(span["section_id"]),
                    span["label_start"],
                    span["label_end"],
                )
            except (CatalogLookupError, ValueError, KeyError, TypeError) as exc:
                raise RosterRevisionError(
                    f"revision {revision_id!r} span {i} invalid: {exc}"
                ) from exc
            resolved.append(
                RosterSpan(
                    section_id=c["section_id"],
                    label_start=c["label_start"],
                    label_end=c["label_end"],
                    physical_start=c["physical"].start,
                    physical_end=c["physical"].end,
                )
            )
            intervals.append(c["physical"])
        merged = merge_intervals(intervals)
        covered = tuple(
            sorted(
                a.asset_id
                for a in self.assets.for_route(self.route_id)
                if covered_by(merged, a.physical_position)
            )
        )

        normalized: list[Shift] = []
        for raw in shifts:
            sh = raw if isinstance(raw, Shift) else make_shift(**raw)
            if sh.facility_id not in covered:
                raise RosterRevisionError(
                    f"shift {sh.shift_id!r} is posted at facility "
                    f"{sh.facility_id!r}, which the revision's spans do not "
                    f"cover {list(covered)}"
                )
            normalized.append(sh)

        revision = RosterRevision(
            revision_id=str(revision_id),
            book_id=self.book_id,
            kind="replace",
            catalog_revision=str(catalog_revision),
            route_id=self.route_id,
            spans=tuple(resolved),
            covered_facilities=covered,
            shifts=tuple(sorted(normalized, key=lambda s: (s.facility_id, s.start, s.shift_id))),
        )
        self._insert(revision)
        return revision

    def add_revoke(
        self,
        revision_id: str,
        catalog_revision: Optional[str] = None,
    ) -> RosterRevision:
        """Add a revocation snapshot: the roster is wholly withdrawn."""
        if catalog_revision is None:
            catalog_revision = (
                self._revisions[-1].catalog_revision
                if self._revisions
                else self.registry.current().revision_id
            )
        revision = RosterRevision(
            revision_id=str(revision_id),
            book_id=self.book_id,
            kind="revoke",
            catalog_revision=str(catalog_revision),
            route_id=self.route_id,
            spans=(),
            covered_facilities=(),
            shifts=(),
        )
        self._insert(revision)
        return revision

    # -- accessors ----------------------------------------------------
    def current(self) -> RosterRevision:
        if not self._revisions:
            raise RosterQueryError(f"book {self.book_id!r} has no revisions")
        return self._revisions[-1]

    def get(self, revision_id: str) -> RosterRevision:
        try:
            return self._by_id[revision_id]
        except KeyError as exc:
            raise RosterQueryError(
                f"revision {revision_id!r} not in book {self.book_id!r}"
            ) from exc

    def has(self, revision_id: str) -> bool:
        return revision_id in self._by_id

    def all_revisions(self) -> list[RosterRevision]:
        return list(self._revisions)

    def lookup(
        self, revision_id: str, facility_id: str, window_start=None, window_end=None
    ) -> dict:
        """On-site snapshot lookup: per-segment staffing and dispute sources."""
        revision = self.get(revision_id)
        segments = facility_timeline(revision, facility_id, window_start, window_end)
        return {
            "book_id": self.book_id,
            "revision_id": revision.revision_id,
            "kind": revision.kind,
            "revoked": revision.revoked,
            "catalog_revision": revision.catalog_revision,
            "facility_id": facility_id,
            "covered": facility_id in revision.covered_facilities,
            "segments": [s.to_dict() for s in segments],
        }


# ---------------------------------------------------------------------------
# SQLite persistence (roster facts, separate from catalogue and inspections)
# ---------------------------------------------------------------------------


ROSTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS roster_book (
    book_id  TEXT PRIMARY KEY,
    route_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roster_revision (
    book_id          TEXT NOT NULL,
    revision_id      TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    kind             TEXT NOT NULL,
    catalog_revision TEXT NOT NULL,
    route_id         TEXT NOT NULL,
    PRIMARY KEY (book_id, revision_id)
);
CREATE TABLE IF NOT EXISTS roster_span (
    book_id        TEXT NOT NULL,
    revision_id    TEXT NOT NULL,
    span_idx       INTEGER NOT NULL,
    section_id     TEXT NOT NULL,
    label_start_num TEXT NOT NULL, label_start_den TEXT NOT NULL,
    label_end_num   TEXT NOT NULL, label_end_den   TEXT NOT NULL,
    phys_start_num  TEXT NOT NULL, phys_start_den  TEXT NOT NULL,
    phys_end_num    TEXT NOT NULL, phys_end_den    TEXT NOT NULL,
    PRIMARY KEY (book_id, revision_id, span_idx)
);
CREATE TABLE IF NOT EXISTS roster_facility (
    book_id     TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    PRIMARY KEY (book_id, revision_id, facility_id)
);
CREATE TABLE IF NOT EXISTS roster_shift (
    book_id     TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    shift_id    TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    person_id   TEXT NOT NULL,
    org_id      TEXT NOT NULL,
    start_num   TEXT NOT NULL, start_den TEXT NOT NULL,
    end_num     TEXT NOT NULL, end_den   TEXT NOT NULL,
    PRIMARY KEY (book_id, revision_id, shift_id)
);
"""


def ensure_roster_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(ROSTER_SCHEMA)


def persist_roster_revision(conn: sqlite3.Connection, revision: RosterRevision) -> None:
    """Store one revision; identical content is idempotent."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO roster_book (book_id, route_id) VALUES (?, ?)",
            (revision.book_id, revision.route_id),
        )
        existing = conn.execute(
            "SELECT kind, catalog_revision FROM roster_revision "
            "WHERE book_id = ? AND revision_id = ?",
            (revision.book_id, revision.revision_id),
        ).fetchone()
        if existing is not None:
            if (
                existing["kind"] != revision.kind
                or existing["catalog_revision"] != revision.catalog_revision
            ):
                raise RosterRevisionError(
                    f"revision {revision.revision_id!r} stored with different content"
                )
            return
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM roster_revision WHERE book_id = ?",
            (revision.book_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO roster_revision "
            "(book_id, revision_id, seq, kind, catalog_revision, route_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                revision.book_id,
                revision.revision_id,
                seq,
                revision.kind,
                revision.catalog_revision,
                revision.route_id,
            ),
        )
        for i, sp in enumerate(revision.spans):
            conn.execute(
                "INSERT INTO roster_span (book_id, revision_id, span_idx, "
                "section_id, label_start_num, label_start_den, label_end_num, "
                "label_end_den, phys_start_num, phys_start_den, phys_end_num, "
                "phys_end_den) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision.book_id, revision.revision_id, i, sp.section_id,
                    str(sp.label_start.numerator), str(sp.label_start.denominator),
                    str(sp.label_end.numerator), str(sp.label_end.denominator),
                    str(sp.physical_start.numerator), str(sp.physical_start.denominator),
                    str(sp.physical_end.numerator), str(sp.physical_end.denominator),
                ),
            )
        for fac in revision.covered_facilities:
            conn.execute(
                "INSERT INTO roster_facility (book_id, revision_id, facility_id) "
                "VALUES (?, ?, ?)",
                (revision.book_id, revision.revision_id, fac),
            )
        for sh in revision.shifts:
            conn.execute(
                "INSERT INTO roster_shift (book_id, revision_id, shift_id, "
                "facility_id, person_id, org_id, start_num, start_den, "
                "end_num, end_den) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision.book_id, revision.revision_id, sh.shift_id,
                    sh.facility_id, sh.person_id, sh.org_id,
                    str(sh.start.numerator), str(sh.start.denominator),
                    str(sh.end.numerator), str(sh.end.denominator),
                ),
            )


def _load_roster_revision(conn: sqlite3.Connection, book_id: str, revision_id: str):
    row = conn.execute(
        "SELECT * FROM roster_revision WHERE book_id = ? AND revision_id = ?",
        (book_id, revision_id),
    ).fetchone()
    if row is None:
        raise RosterQueryError(
            f"revision {revision_id!r} not stored for book {book_id!r}"
        )
    spans = [
        RosterSpan(
            section_id=r["section_id"],
            label_start=Fraction(int(r["label_start_num"]), int(r["label_start_den"])),
            label_end=Fraction(int(r["label_end_num"]), int(r["label_end_den"])),
            physical_start=Fraction(int(r["phys_start_num"]), int(r["phys_start_den"])),
            physical_end=Fraction(int(r["phys_end_num"]), int(r["phys_end_den"])),
        )
        for r in conn.execute(
            "SELECT * FROM roster_span WHERE book_id = ? AND revision_id = ? "
            "ORDER BY span_idx",
            (book_id, revision_id),
        ).fetchall()
    ]
    covered = tuple(
        r["facility_id"]
        for r in conn.execute(
            "SELECT facility_id FROM roster_facility WHERE book_id = ? "
            "AND revision_id = ? ORDER BY facility_id",
            (book_id, revision_id),
        ).fetchall()
    )
    shifts = [
        Shift(
            shift_id=r["shift_id"],
            facility_id=r["facility_id"],
            person_id=r["person_id"],
            org_id=r["org_id"],
            start=Fraction(int(r["start_num"]), int(r["start_den"])),
            end=Fraction(int(r["end_num"]), int(r["end_den"])),
        )
        for r in conn.execute(
            "SELECT * FROM roster_shift WHERE book_id = ? AND revision_id = ? "
            "ORDER BY facility_id, start_num, shift_id",
            (book_id, revision_id),
        ).fetchall()
    ]
    return RosterRevision(
        revision_id=row["revision_id"],
        book_id=row["book_id"],
        kind=row["kind"],
        catalog_revision=row["catalog_revision"],
        route_id=row["route_id"],
        spans=tuple(spans),
        covered_facilities=covered,
        shifts=tuple(shifts),
    )


def load_roster_book(
    conn: sqlite3.Connection,
    book_id: str,
    registry: CatalogRegistry,
    assets: AssetsIndex,
) -> RosterBook:
    """Rehydrate a book (all revisions, newest last) from SQLite."""
    row = conn.execute(
        "SELECT route_id FROM roster_book WHERE book_id = ?", (book_id,)
    ).fetchone()
    if row is None:
        raise RosterQueryError(f"book {book_id!r} not stored")
    book = RosterBook(book_id, row["route_id"], registry, assets)
    for r in conn.execute(
        "SELECT revision_id FROM roster_revision WHERE book_id = ? ORDER BY seq",
        (book_id,),
    ).fetchall():
        book._insert(_load_roster_revision(conn, book_id, r["revision_id"]))
    return book


def persist_roster_book(conn: sqlite3.Connection, book: RosterBook) -> None:
    for revision in book.all_revisions():
        persist_roster_revision(conn, revision)
