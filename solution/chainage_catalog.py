"""Independent route chainage catalog.

Design notes (see README for the fuller discussion):

A route is a physical axis ``[0, L)`` tiled by positive-length half-open
segments ``[physical_start, physical_end)`` with stable ``section_id``s.
Within a section the chainage label is

    label = physical - physical_start + label_start        (slope fixed at 1)

so ``physical = label - label_start + physical_start``.

Labels are *not* a global coordinate: they may repeat across sections and
may have gaps.  A label point lookup therefore returns *all* candidate
``(section_id, physical)`` pairs and never silently picks a nearest value:

* 0 candidates -> ``unresolved``
* >1 candidates -> ``ambiguous`` (cannot be resolved by this module)
* exactly 1    -> ``resolved``

Half-open semantics are used uniformly.  A point exactly on a segment's
right end does **not** belong to that segment.  An interval's *upper bound*
may equal a segment's end label, though -- the endpoint is a boundary, not a
member point.  Mixing those two up is exactly the bug that rejects a legal
end-of-section span.

Numbers are held as :class:`fractions.Fraction` so exact boundary
comparisons are possible; SQLite stores them as ``'n/d'`` text and has a
deterministic sortable column so SQL-side queries still work.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Optional, Sequence

# ---------------------------------------------------------------------------
# Exact rational helpers
# ---------------------------------------------------------------------------


def as_fraction(value) -> Fraction:
    """Parse ints/floats/strings/'n/d' into an exact Fraction.

    Floats are converted through their shortest decimal string so callers
    passing ``350.0`` get the rational they meant, not the binary artifact.
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise TypeError("bool is not a valid coordinate")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        # repr gives the shortest round-tripping decimal.
        return Fraction(repr(value))
    if isinstance(value, str):
        return Fraction(value)
    raise TypeError(f"cannot interpret {value!r} as a rational coordinate")


def fraction_to_db(value: Fraction) -> str:
    """Canonical ``'n/d'`` text form (d always positive)."""
    value = as_fraction(value)
    return f"{value.numerator}/{value.denominator}"


def fraction_from_db(text: str) -> Fraction:
    return Fraction(text)


def sortable_key(value: Fraction) -> str:
    """A lexicographically sortable encoding of a Fraction.

    Used only so a SQL index can be kept over exact rationals without float
    precision loss.  The Python side always compares exact Fractions; this
    key is a convenience for ``ORDER BY`` / indexed range prefiltering.
    """
    value = as_fraction(value)
    scale = 10**18
    scaled = value * scale
    whole = scaled.numerator // scaled.denominator
    rem = scaled - whole
    if rem < 0:
        raise ValueError("sortable_key only supports non-negative values")
    frac_key = f"{rem.numerator:018d}".rstrip("0") or "0"
    return f"{whole:020d}.{frac_key}"


# ---------------------------------------------------------------------------
# Core model (in-memory, versioned, independent of SQLite)
# ---------------------------------------------------------------------------


class RouteDefinitionError(ValueError):
    """Raised when a route fails structural validation."""


class CatalogLookupError(KeyError):
    """Raised when a revision/section is not present in the catalog."""


@dataclass(frozen=True)
class Section:
    section_id: str
    physical_start: Fraction
    physical_end: Fraction
    label_start: Fraction

    @property
    def physical_length(self) -> Fraction:
        return self.physical_end - self.physical_start

    def label_at(self, physical: Fraction) -> Fraction:
        return physical - self.physical_start + self.label_start

    def physical_at(self, label: Fraction) -> Fraction:
        return label - self.label_start + self.physical_start

    @property
    def label_end(self) -> Fraction:
        # First label *not* in the section.
        return self.label_at(self.physical_end)

    def contains_physical_point(self, physical: Fraction) -> bool:
        """Half-open membership: left end included, right end excluded."""
        return self.physical_start <= physical < self.physical_end

    def label_interval_for(self, label_start: Fraction, label_end: Fraction):
        """Map a label half-open interval wholly inside this section.

        Returns ``(physical_start, physical_end)`` or raises ``ValueError``
        with a reason.  The upper bound may equal ``label_end`` (the
        section's exclusive end label): it is a boundary, not a member.
        """
        if not label_start < label_end:
            raise ValueError(
                f"label span [{label_start}, {label_end}) is not positive length"
            )
        lo = self.label_at(self.physical_start)  # == label_start attr
        hi = self.label_end
        if label_start < lo:
            raise ValueError(
                f"label span start {label_start} is before section "
                f"{self.section_id} label range [{lo}, {hi})"
            )
        if label_end > hi:
            raise ValueError(
                f"label span end {label_end} is past section "
                f"{self.section_id} label range [{lo}, {hi})"
            )
        # Endpoints are converted as boundaries, so label_end == hi maps to
        # physical_end and yields a perfect half-open physical interval.
        phys_lo = self.physical_at(label_start)
        phys_hi = self.physical_at(label_end)
        return phys_lo, phys_hi


@dataclass(frozen=True)
class Interval:
    """Half-open physical interval on the route axis."""

    start: Fraction
    end: Fraction

    def __post_init__(self):
        if not self.start < self.end:
            raise ValueError("interval must have positive length")

    @property
    def length(self) -> Fraction:
        return self.end - self.start

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Merge overlapping **and touching** intervals; returns sorted union.

    The union has no duplicated length: ``sum(result.length)`` is the true
    covered measure even when the input had overlapping or repeated spans.
    """
    ordered = sorted(intervals, key=lambda iv: (iv.start, iv.end))
    merged: list[Interval] = []
    for iv in ordered:
        if merged and iv.start <= merged[-1].end:
            prev = merged[-1]
            if iv.end > prev.end:
                merged[-1] = Interval(prev.start, iv.end)
            # else fully contained: nothing to add
        else:
            merged.append(iv)
    return merged


class RouteCatalog:
    """Immutable, validated single-revision view of a route."""

    def __init__(
        self,
        revision_id: str,
        route_id: str,
        length,
        sections: Sequence[Section],
    ):
        self.revision_id = revision_id
        self.route_id = route_id
        self.length = as_fraction(length)
        self.sections = tuple(sections)
        self._by_id = {s.section_id: s for s in self.sections}
        self._validate()

    # -- construction -------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict) -> "RouteCatalog":
        sections = [
            Section(
                section_id=str(s["section_id"]),
                physical_start=as_fraction(s["physical_start"]),
                physical_end=as_fraction(s["physical_end"]),
                label_start=as_fraction(s["label_start"]),
            )
            for s in data["sections"]
        ]
        return cls(
            revision_id=str(data["revision"]),
            route_id=str(data["route_id"]),
            length=data["length"],
            sections=sections,
        )

    def _validate(self) -> None:
        if self.length <= 0:
            raise RouteDefinitionError("route length must be positive")
        ids = [s.section_id for s in self.sections]
        if len(ids) != len(set(ids)):
            raise RouteDefinitionError("duplicate section_id in route")
        # Each section must be positive length.
        for s in self.sections:
            if s.physical_length <= 0:
                raise RouteDefinitionError(
                    f"section {s.section_id} has non-positive length"
                )
        # Sections must tile [0, L) contiguously with no gap or overlap.
        ordered = sorted(self.sections, key=lambda s: s.physical_start)
        cursor = Fraction(0)
        for s in ordered:
            if s.physical_start != cursor:
                raise RouteDefinitionError(
                    f"section {s.section_id} starts at {s.physical_start}, "
                    f"expected contiguous boundary {cursor}"
                )
            cursor = s.physical_end
        if cursor != self.length:
            raise RouteDefinitionError(
                f"sections end at {cursor}, route length is {self.length}"
            )

    # -- accessors ----------------------------------------------------
    def section(self, section_id: str) -> Section:
        try:
            return self._by_id[section_id]
        except KeyError as exc:
            raise CatalogLookupError(
                f"section {section_id!r} not in revision {self.revision_id}"
            ) from exc

    def has_section(self, section_id: str) -> bool:
        return section_id in self._by_id

    # -- point reverse lookup ----------------------------------------
    def resolve_label_point(self, label, section_id: Optional[str] = None) -> dict:
        """Return every candidate ``(section_id, physical)`` for a label.

        Never picks a nearest winner.  When ``section_id`` is supplied the
        lookup is restricted to that section (an explicit disambiguation the
        caller is entitled to make); an unknown section yields an empty
        candidate set rather than an error.  Result ``status`` is one of
        ``resolved`` / ``ambiguous`` / ``unresolved``.
        """
        label = as_fraction(label)
        if section_id is not None and not self.has_section(section_id):
            return {
                "label": label,
                "section_id": section_id,
                "status": "unresolved",
                "candidates": [],
            }
        candidates = []
        for s in self.sections:
            if section_id is not None and s.section_id != section_id:
                continue
            lo = s.label_at(s.physical_start)
            hi = s.label_end
            if lo <= label < hi:  # half-open: right end excluded
                candidates.append(
                    {
                        "section_id": s.section_id,
                        "physical": s.physical_at(label),
                    }
                )
        if len(candidates) == 1:
            status = "resolved"
        elif len(candidates) == 0:
            status = "unresolved"
        else:
            status = "ambiguous"
        result = {"label": label, "status": status, "candidates": candidates}
        if section_id is not None:
            result["section_id"] = section_id
        return result

    # -- interval conversion -----------------------------------------
    def convert_span(self, section_id: str, label_start, label_end) -> dict:
        """Convert one label span to a physical :class:`Interval`.

        Raises ``CatalogLookupError`` for an unknown section and
        ``ValueError`` for a malformed / out-of-section / non-positive span.
        """
        section = self.section(section_id)
        ls = as_fraction(label_start)
        le = as_fraction(label_end)
        phys_lo, phys_hi = section.label_interval_for(ls, le)
        return {
            "section_id": section_id,
            "label_start": ls,
            "label_end": le,
            "physical": Interval(phys_lo, phys_hi),
        }

    def convert_spans(self, spans: Iterable[dict]) -> list[dict]:
        """Convert a batch; raises on the first invalid span (caller aborts)."""
        out = []
        for span in spans:
            out.append(
                self.convert_span(
                    str(span["section_id"]),
                    span["label_start"],
                    span["label_end"],
                )
            )
        return out


# ---------------------------------------------------------------------------
# Version registry
# ---------------------------------------------------------------------------


class CatalogRegistry:
    """Ordered set of route revisions; newest revision is ``current()``."""

    def __init__(self):
        self._revisions: list[RouteCatalog] = []
        self._by_id: dict[str, RouteCatalog] = {}

    def add(self, catalog: RouteCatalog) -> None:
        if catalog.revision_id in self._by_id:
            raise RouteDefinitionError(
                f"revision {catalog.revision_id!r} already registered"
            )
        self._revisions.append(catalog)
        self._by_id[catalog.revision_id] = catalog

    def get(self, revision_id: str) -> RouteCatalog:
        try:
            return self._by_id[revision_id]
        except KeyError as exc:
            raise CatalogLookupError(f"unknown revision {revision_id!r}") from exc

    def current(self) -> RouteCatalog:
        if not self._revisions:
            raise CatalogLookupError("no revisions registered")
        return self._revisions[-1]

    def all_revisions(self) -> list[RouteCatalog]:
        return list(self._revisions)

    def revise_section_label_start(
        self, revision_id: str, route_id: str, section_id: str, label_start
    ) -> RouteCatalog:
        """Produce a new revision by changing one section's base label.

        Deliberately narrow: only ``label_start`` moves.  Physical extents,
        section order and all other sections are copied verbatim.  This is
        *not* a route editor.
        """
        prev = self.current()
        if prev.route_id != route_id:
            raise RouteDefinitionError(
                f"revision {revision_id!r} targets route {route_id!r}, "
                f"current route is {prev.route_id!r}"
            )
        if not prev.has_section(section_id):
            raise CatalogLookupError(
                f"section {section_id!r} not in revision {prev.revision_id}"
            )
        new_label = as_fraction(label_start)
        new_sections = [
            Section(
                section_id=s.section_id,
                physical_start=s.physical_start,
                physical_end=s.physical_end,
                label_start=new_label if s.section_id == section_id else s.label_start,
            )
            for s in prev.sections
        ]
        new_cat = RouteCatalog(
            revision_id=revision_id,
            route_id=prev.route_id,
            length=prev.length,
            sections=new_sections,
        )
        self.add(new_cat)
        return new_cat


# ---------------------------------------------------------------------------
# SQLite catalogue storage (independent of inspection facts)
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS route_revision (
    revision_id  TEXT PRIMARY KEY,
    route_id     TEXT NOT NULL,
    length_num   TEXT NOT NULL,
    length_den   TEXT NOT NULL,
    created_seq  INTEGER NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS route_section (
    revision_id    TEXT NOT NULL REFERENCES route_revision(revision_id),
    section_id     TEXT NOT NULL,
    section_order  INTEGER NOT NULL,
    phys_start_num TEXT NOT NULL,
    phys_start_den TEXT NOT NULL,
    phys_start_key TEXT NOT NULL,
    phys_end_num   TEXT NOT NULL,
    phys_end_den   TEXT NOT NULL,
    phys_end_key   TEXT NOT NULL,
    label_start_num TEXT NOT NULL,
    label_start_den TEXT NOT NULL,
    -- Index for the section_id join and for ordered scans.
    PRIMARY KEY (revision_id, section_id)
);
CREATE INDEX IF NOT EXISTS idx_section_phys
    ON route_section (revision_id, phys_start_key);
"""


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _store_fraction_pair(values: Sequence[Fraction], prefix: str) -> dict:
    v = values
    return {
        f"{prefix}_num": str(v.numerator),
        f"{prefix}_den": str(v.denominator),
    }


def persist_catalog(conn: sqlite3.Connection, catalog: RouteCatalog) -> None:
    """Persist a single revision (idempotent on revision_id)."""
    with conn:  # transaction
        try:
            conn.execute(
                "INSERT INTO route_revision "
                "(revision_id, route_id, length_num, length_den, created_seq) "
                "VALUES (?, ?, ?, ?, "
                "(SELECT COALESCE(MAX(created_seq), 0) + 1 FROM route_revision))",
                (
                    catalog.revision_id,
                    catalog.route_id,
                    str(catalog.length.numerator),
                    str(catalog.length.denominator),
                ),
            )
        except sqlite3.IntegrityError:
            # Already stored: verify it matches, else refuse.
            existing = load_catalog(conn, catalog.revision_id)
            if (
                existing.length != catalog.length
                or [(s.section_id, s.physical_start, s.physical_end, s.label_start)
                    for s in existing.sections]
                != [(s.section_id, s.physical_start, s.physical_end, s.label_start)
                    for s in catalog.sections]
            ):
                raise RouteDefinitionError(
                    f"revision {catalog.revision_id!r} stored with different content"
                )
            return
        for order, s in enumerate(catalog.sections):
            conn.execute(
                "INSERT INTO route_section (revision_id, section_id, section_order, "
                "phys_start_num, phys_start_den, phys_start_key, "
                "phys_end_num, phys_end_den, phys_end_key, "
                "label_start_num, label_start_den) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    catalog.revision_id,
                    s.section_id,
                    order,
                    str(s.physical_start.numerator),
                    str(s.physical_start.denominator),
                    sortable_key(s.physical_start),
                    str(s.physical_end.numerator),
                    str(s.physical_end.denominator),
                    sortable_key(s.physical_end),
                    str(s.label_start.numerator),
                    str(s.label_start.denominator),
                ),
            )


def load_catalog(conn: sqlite3.Connection, revision_id: str) -> RouteCatalog:
    row = conn.execute(
        "SELECT * FROM route_revision WHERE revision_id = ?", (revision_id,)
    ).fetchone()
    if row is None:
        raise CatalogLookupError(f"revision {revision_id!r} not stored")
    section_rows = conn.execute(
        "SELECT * FROM route_section WHERE revision_id = ? ORDER BY section_order",
        (revision_id,),
    ).fetchall()
    sections = [
        Section(
            section_id=r["section_id"],
            physical_start=Fraction(int(r["phys_start_num"]), int(r["phys_start_den"])),
            physical_end=Fraction(int(r["phys_end_num"]), int(r["phys_end_den"])),
            label_start=Fraction(int(r["label_start_num"]), int(r["label_start_den"])),
        )
        for r in section_rows
    ]
    return RouteCatalog(
        revision_id=row["revision_id"],
        route_id=row["route_id"],
        length=Fraction(int(row["length_num"]), int(row["length_den"])),
        sections=sections,
    )


def load_catalog_registry(conn: sqlite3.Connection) -> CatalogRegistry:
    reg = CatalogRegistry()
    rows = conn.execute(
        "SELECT revision_id FROM route_revision ORDER BY created_seq"
    ).fetchall()
    for r in rows:
        reg.add(load_catalog(conn, r["revision_id"]))
    return reg


def load_latest_catalog(conn: sqlite3.Connection) -> Optional[RouteCatalog]:
    row = conn.execute(
        "SELECT revision_id FROM route_revision ORDER BY created_seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return load_catalog(conn, row["revision_id"])
