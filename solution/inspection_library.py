"""Inspection coverage facts, report versioning and facility identity.

This module deliberately depends on :mod:`chainage_catalog` and never the
other way round: the catalogue can resolve points and convert intervals
without ever knowing a job exists.

Key invariants
--------------
* A span must name its ``section_id`` and a positive-length half-open label
  interval wholly inside that section.  A span's *upper bound* may equal the
  section's exclusive end label.
* Spans are converted to physical intervals and merged, so the covered
  length is the measure of the union -- overlapping or repeated spans are
  never double-counted.
* Facilities are hit only when their **physical** position falls inside the
  merged half-open coverage.  No coverage is ever derived from an
  unresolved / ambiguous label point, so an ambiguous point can never
  manufacture a facility hit.
* If any span of a job is invalid, the job is not published at all.
* Every report records the original label request, the selected sections,
  the actual physical coverage, the hit facility identities and the
  catalogue revision it was resolved against.
* ``report_id`` is content-addressed: re-reading identical content is
  idempotent; reusing the same id for different content is rejected.
* A newer catalogue revision makes older reports *stale* for re-use but
  their stored content still explains the original labels and positions.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable

from chainage_catalog import (
    CatalogLookupError,
    CatalogRegistry,
    Interval,
    as_fraction,
    fraction_to_db,
    merge_intervals,
)


class InspectionError(ValueError):
    """Base error for inspection processing."""


class InvalidSpanError(InspectionError):
    """A job span could not be resolved; the whole job is rejected."""


class ReportConflictError(InspectionError):
    """A report_id was reused with different content."""


# ---------------------------------------------------------------------------
# Assets (fixed physical identities)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Asset:
    asset_id: str
    route_id: str
    physical_position: Fraction

    @classmethod
    def from_dict(cls, data: dict) -> "Asset":
        return cls(
            asset_id=str(data["asset_id"]),
            route_id=str(data["route_id"]),
            physical_position=as_fraction(data["physical_position"]),
        )


# ---------------------------------------------------------------------------
# Report value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportSpan:
    section_id: str
    label_start: Fraction
    label_end: Fraction
    physical_start: Fraction
    physical_end: Fraction

    def to_canonical(self) -> dict:
        return {
            "section_id": self.section_id,
            "label_start": fraction_to_db(self.label_start),
            "label_end": fraction_to_db(self.label_end),
            "physical_start": fraction_to_db(self.physical_start),
            "physical_end": fraction_to_db(self.physical_end),
        }


@dataclass(frozen=True)
class FacilityHit:
    asset_id: str
    physical_position: Fraction

    def to_canonical(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "physical_position": fraction_to_db(self.physical_position),
        }


@dataclass(frozen=True)
class Report:
    report_id: str
    route_id: str
    job_id: str
    catalog_revision: str
    requested_spans: tuple[ReportSpan, ...]
    merged: tuple[Interval, ...]
    covered_length: Fraction
    facilities: tuple[FacilityHit, ...]

    # -- canonical content -------------------------------------------
    def canonical_content(self) -> dict:
        """Everything that identifies the report, excluding its own id."""
        return {
            "route_id": self.route_id,
            "job_id": self.job_id,
            "catalog_revision": self.catalog_revision,
            "requested_spans": [s.to_canonical() for s in self.requested_spans],
            "merged_coverage": [
                {"start": fraction_to_db(iv.start), "end": fraction_to_db(iv.end)}
                for iv in self.merged
            ],
            "covered_length": fraction_to_db(self.covered_length),
            "facilities": [f.to_canonical() for f in self.facilities],
        }

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.canonical_content(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        data = self.canonical_content()
        data["report_id"] = self.report_id
        data["fingerprint"] = self.fingerprint()
        return data


# ---------------------------------------------------------------------------
# Coverage helpers
# ---------------------------------------------------------------------------


def covered_by(merged: list[Interval], point: Fraction) -> bool:
    """Half-open membership test against a sorted, disjoint union."""
    starts = [iv.start for iv in merged]
    idx = bisect.bisect_right(starts, point) - 1
    if idx < 0:
        return False
    return point < merged[idx].end


class AssetsIndex:
    """Assets grouped by route, with their positions kept fixed."""

    def __init__(self, assets: Iterable[Asset]):
        self._assets = tuple(assets)
        seen = set()
        self._by_route: dict[str, list[Asset]] = {}
        for a in self._assets:
            if a.asset_id in seen:
                raise InspectionError(f"duplicate asset_id {a.asset_id!r}")
            seen.add(a.asset_id)
            self._by_route.setdefault(a.route_id, []).append(a)

    def for_route(self, route_id: str) -> list[Asset]:
        return list(self._by_route.get(route_id, []))

    def __iter__(self):
        return iter(self._assets)

    def __len__(self):
        return len(self._assets)


# ---------------------------------------------------------------------------
# Inspection library
# ---------------------------------------------------------------------------


class InspectionLibrary:
    """Holds catalogue revisions, fixed assets and published reports."""

    def __init__(self, registry: CatalogRegistry, assets: Iterable[Asset]):
        self.registry = registry
        self.assets = AssetsIndex(assets)
        self._reports: dict[str, Report] = {}

    # -- report construction -----------------------------------------
    def resolve_and_inspect(self, job: dict, report_id: str) -> Report:
        """Resolve a job against the *current* catalogue revision.

        The whole job is rejected if any span is invalid.  Nothing is stored
        until every span has converted successfully.
        """
        if not report_id:
            raise InspectionError("report_id must be non-empty")
        route_id = str(job["route_id"])
        job_id = str(job["job_id"])
        catalog = self.registry.current()
        if catalog.route_id != route_id:
            raise InspectionError(
                f"job {job_id!r} targets route {route_id!r}, "
                f"current revision {catalog.revision_id!r} is route "
                f"{catalog.route_id!r}"
            )

        spans = job.get("spans")
        if not spans:
            raise InvalidSpanError(f"job {job_id!r} has no spans")

        converted: list[dict] = []
        # Convert all spans first; any failure aborts before publication.
        for i, span in enumerate(spans):
            try:
                converted.append(
                    catalog.convert_span(
                        str(span["section_id"]),
                        span["label_start"],
                        span["label_end"],
                    )
                )
            except (CatalogLookupError, ValueError, KeyError, TypeError) as exc:
                raise InvalidSpanError(
                    f"job {job_id!r} span {i} invalid; job not published: {exc}"
                ) from exc

        merged = merge_intervals(c["physical"] for c in converted)
        covered_length = sum((iv.length for iv in merged), Fraction(0))

        hits = self._facility_hits(route_id, merged)

        report = Report(
            report_id=report_id,
            route_id=route_id,
            job_id=job_id,
            catalog_revision=catalog.revision_id,
            requested_spans=tuple(
                ReportSpan(
                    section_id=c["section_id"],
                    label_start=c["label_start"],
                    label_end=c["label_end"],
                    physical_start=c["physical"].start,
                    physical_end=c["physical"].end,
                )
                for c in converted
            ),
            merged=tuple(merged),
            covered_length=covered_length,
            facilities=tuple(hits),
        )

        self._publish(report)
        return report

    def _facility_hits(
        self, route_id: str, merged: list[Interval]
    ) -> list[FacilityHit]:
        hits = []
        for asset in self.assets.for_route(route_id):
            # Physical membership only -- never a label lookup.
            if covered_by(merged, asset.physical_position):
                hits.append(
                    FacilityHit(asset.asset_id, asset.physical_position)
                )
        hits.sort(key=lambda h: h.asset_id)
        return hits

    def _publish(self, report: Report) -> None:
        existing = self._reports.get(report.report_id)
        if existing is not None:
            if existing.fingerprint() != report.fingerprint():
                raise ReportConflictError(
                    f"report_id {report.report_id!r} already exists with "
                    f"different content"
                )
            # Identical content: idempotent no-op.
            return
        self._reports[report.report_id] = report

    # -- reads --------------------------------------------------------
    def get_report(self, report_id: str) -> Report:
        try:
            return self._reports[report_id]
        except KeyError as exc:
            raise KeyError(f"no report {report_id!r}") from exc

    def report_view(self, report_id: str) -> dict:
        """Render a stored report, including its staleness status.

        A report is *stale* when a newer revision of the same route exists;
        its stored content is still returned unchanged so it continues to
        explain the labels and positions it was resolved against.
        """
        report = self.get_report(report_id)
        catalog = self.registry.get(report.catalog_revision)
        current = self.registry.current()
        stale = (
            current.route_id == catalog.route_id
            and current.revision_id != catalog.revision_id
        )
        data = report.to_dict()
        data["stale"] = stale
        data["current_revision"] = current.revision_id
        return data

    def is_current(self, report_id: str) -> bool:
        report = self.get_report(report_id)
        current = self.registry.current()
        return current.route_id == report.route_id and (
            current.revision_id == report.catalog_revision
        )

    def reports_affected_by(self, new_revision_id: str) -> list[str]:
        """Report ids that become stale if ``new_revision_id`` is current."""
        current = self.registry.get(new_revision_id)
        return [
            rid
            for rid, rep in self._reports.items()
            if rep.route_id == current.route_id
            and rep.catalog_revision != new_revision_id
        ]


# ---------------------------------------------------------------------------
# SQLite persistence for inspection facts (separate schema from catalogue)
# ---------------------------------------------------------------------------


INSPECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS asset (
    asset_id   TEXT PRIMARY KEY,
    route_id   TEXT NOT NULL,
    phys_num   TEXT NOT NULL,
    phys_den   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report (
    report_id        TEXT PRIMARY KEY,
    route_id         TEXT NOT NULL,
    job_id           TEXT NOT NULL,
    catalog_revision TEXT NOT NULL,
    covered_len_num  TEXT NOT NULL,
    covered_len_den  TEXT NOT NULL,
    fingerprint      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_span (
    report_id      TEXT NOT NULL REFERENCES report(report_id),
    span_idx       INTEGER NOT NULL,
    section_id     TEXT NOT NULL,
    label_start_num TEXT NOT NULL, label_start_den TEXT NOT NULL,
    label_end_num   TEXT NOT NULL, label_end_den   TEXT NOT NULL,
    phys_start_num  TEXT NOT NULL, phys_start_den  TEXT NOT NULL,
    phys_end_num    TEXT NOT NULL, phys_end_den    TEXT NOT NULL,
    PRIMARY KEY (report_id, span_idx)
);
CREATE TABLE IF NOT EXISTS report_interval (
    report_id  TEXT NOT NULL REFERENCES report(report_id),
    iv_idx     INTEGER NOT NULL,
    start_num  TEXT NOT NULL, start_den TEXT NOT NULL,
    end_num    TEXT NOT NULL, end_den   TEXT NOT NULL,
    PRIMARY KEY (report_id, iv_idx)
);
CREATE TABLE IF NOT EXISTS report_facility (
    report_id  TEXT NOT NULL REFERENCES report(report_id),
    asset_id   TEXT NOT NULL,
    phys_num   TEXT NOT NULL,
    phys_den   TEXT NOT NULL,
    PRIMARY KEY (report_id, asset_id)
);
-- Durable op-log so a file-backed database resumes instead of replaying.
CREATE TABLE IF NOT EXISTS applied_op (
    op_seq     INTEGER PRIMARY KEY,
    op_hash    TEXT NOT NULL
);
"""


def ensure_inspection_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(INSPECTION_SCHEMA)


def persist_assets(conn: sqlite3.Connection, assets: Iterable[Asset]) -> None:
    with conn:
        for a in assets:
            conn.execute(
                "INSERT OR REPLACE INTO asset (asset_id, route_id, phys_num, phys_den) "
                "VALUES (?, ?, ?, ?)",
                (
                    a.asset_id,
                    a.route_id,
                    str(a.physical_position.numerator),
                    str(a.physical_position.denominator),
                ),
            )


def load_assets(conn: sqlite3.Connection) -> list[Asset]:
    rows = conn.execute(
        "SELECT * FROM asset ORDER BY asset_id"
    ).fetchall()
    return [
        Asset(
            asset_id=r["asset_id"],
            route_id=r["route_id"],
            physical_position=Fraction(int(r["phys_num"]), int(r["phys_den"])),
        )
        for r in rows
    ]


def persist_report(conn: sqlite3.Connection, report: Report) -> None:
    """Store a report; identical content is idempotent, differing content fails."""
    with conn:
        existing = conn.execute(
            "SELECT fingerprint FROM report WHERE report_id = ?",
            (report.report_id,),
        ).fetchone()
        if existing is not None:
            if existing["fingerprint"] != report.fingerprint():
                raise ReportConflictError(
                    f"report_id {report.report_id!r} stored with different content"
                )
            return
        conn.execute(
            "INSERT INTO report (report_id, route_id, job_id, catalog_revision, "
            "covered_len_num, covered_len_den, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                report.report_id,
                report.route_id,
                report.job_id,
                report.catalog_revision,
                str(report.covered_length.numerator),
                str(report.covered_length.denominator),
                report.fingerprint(),
            ),
        )
        for i, s in enumerate(report.requested_spans):
            conn.execute(
                "INSERT INTO report_span (report_id, span_idx, section_id, "
                "label_start_num, label_start_den, label_end_num, label_end_den, "
                "phys_start_num, phys_start_den, phys_end_num, phys_end_den) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report.report_id, i, s.section_id,
                    str(s.label_start.numerator), str(s.label_start.denominator),
                    str(s.label_end.numerator), str(s.label_end.denominator),
                    str(s.physical_start.numerator), str(s.physical_start.denominator),
                    str(s.physical_end.numerator), str(s.physical_end.denominator),
                ),
            )
        for i, iv in enumerate(report.merged):
            conn.execute(
                "INSERT INTO report_interval (report_id, iv_idx, "
                "start_num, start_den, end_num, end_den) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    report.report_id, i,
                    str(iv.start.numerator), str(iv.start.denominator),
                    str(iv.end.numerator), str(iv.end.denominator),
                ),
            )
        for f in report.facilities:
            conn.execute(
                "INSERT INTO report_facility (report_id, asset_id, phys_num, phys_den) "
                "VALUES (?, ?, ?, ?)",
                (
                    report.report_id, f.asset_id,
                    str(f.physical_position.numerator),
                    str(f.physical_position.denominator),
                ),
            )


def load_report(conn: sqlite3.Connection, report_id: str) -> Report:
    row = conn.execute(
        "SELECT * FROM report WHERE report_id = ?", (report_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no report {report_id!r}")
    spans = [
        ReportSpan(
            section_id=r["section_id"],
            label_start=Fraction(int(r["label_start_num"]), int(r["label_start_den"])),
            label_end=Fraction(int(r["label_end_num"]), int(r["label_end_den"])),
            physical_start=Fraction(int(r["phys_start_num"]), int(r["phys_start_den"])),
            physical_end=Fraction(int(r["phys_end_num"]), int(r["phys_end_den"])),
        )
        for r in conn.execute(
            "SELECT * FROM report_span WHERE report_id = ? ORDER BY span_idx",
            (report_id,),
        ).fetchall()
    ]
    merged = [
        Interval(
            Fraction(int(r["start_num"]), int(r["start_den"])),
            Fraction(int(r["end_num"]), int(r["end_den"])),
        )
        for r in conn.execute(
            "SELECT * FROM report_interval WHERE report_id = ? ORDER BY iv_idx",
            (report_id,),
        ).fetchall()
    ]
    facilities = [
        FacilityHit(
            asset_id=r["asset_id"],
            physical_position=Fraction(int(r["phys_num"]), int(r["phys_den"])),
        )
        for r in conn.execute(
            "SELECT * FROM report_facility WHERE report_id = ? ORDER BY asset_id",
            (report_id,),
        ).fetchall()
    ]
    return Report(
        report_id=row["report_id"],
        route_id=row["route_id"],
        job_id=row["job_id"],
        catalog_revision=row["catalog_revision"],
        requested_spans=tuple(spans),
        merged=tuple(merged),
        covered_length=Fraction(int(row["covered_len_num"]), int(row["covered_len_den"])),
        facilities=tuple(facilities),
    )


def load_all_reports(conn: sqlite3.Connection) -> dict[str, Report]:
    ids = [r["report_id"] for r in conn.execute("SELECT report_id FROM report")]
    return {rid: load_report(conn, rid) for rid in ids}
