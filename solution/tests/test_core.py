"""Edge-case tests for the chainage catalog and inspection facts.

Run:  python3 -m unittest discover -s solution/tests -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chainage_catalog
from chainage_catalog import (  # noqa: E402
    CatalogLookupError,
    CatalogRegistry,
    Interval,
    RouteCatalog,
    RouteDefinitionError,
    Section,
    load_catalog_registry,
    merge_intervals,
    open_db,
    persist_catalog,
)
from inspection_library import (  # noqa: E402
    Asset,
    InspectionError,
    InspectionLibrary,
    InvalidSpanError,
    ReportConflictError,
    covered_by,
    ensure_inspection_schema,
    load_report,
    persist_assets,
    persist_report,
)


def route_r1() -> RouteCatalog:
    """The materials/730 route, hand-built for precision."""
    return RouteCatalog(
        revision_id="r1",
        route_id="L",
        length=1000,
        sections=[
            Section("P1", Fraction(0), Fraction(400), Fraction(0)),
            Section("P2", Fraction(400), Fraction(700), Fraction(300)),
            Section("P3", Fraction(700), Fraction(1000), Fraction(650)),
        ],
    )


def assets_abc() -> list[Asset]:
    return [
        Asset("A", "L", Fraction(350)),
        Asset("B", "L", Fraction(470)),
        Asset("C", "L", Fraction(440)),
    ]


def fresh_lib(revision: str = "r1") -> InspectionLibrary:
    reg = CatalogRegistry()
    cat = route_r1()
    if revision != "r1":
        reg.add(cat)
        cat = reg.revise_section_label_start(revision, "L", "P2", 330)
        return InspectionLibrary(reg, assets_abc())
    reg.add(cat)
    return InspectionLibrary(reg, assets_abc())


# ---------------------------------------------------------------------------
# Catalog structural validation
# ---------------------------------------------------------------------------


class TestRouteValidation(unittest.TestCase):
    def test_contiguous_tiling_required(self):
        with self.assertRaises(RouteDefinitionError):
            RouteCatalog(
                "x", "L", 1000,
                [
                    Section("A", Fraction(0), Fraction(400), Fraction(0)),
                    Section("B", Fraction(401), Fraction(700), Fraction(0)),
                    Section("C", Fraction(700), Fraction(1000), Fraction(0)),
                ],
            )

    def test_must_cover_full_length(self):
        with self.assertRaises(RouteDefinitionError):
            RouteCatalog(
                "x", "L", 1000,
                [Section("A", Fraction(0), Fraction(999), Fraction(0))],
            )

    def test_zero_length_section_rejected(self):
        with self.assertRaises(RouteDefinitionError):
            RouteCatalog(
                "x", "L", 1000,
                [
                    Section("A", Fraction(0), Fraction(400), Fraction(0)),
                    Section("Z", Fraction(400), Fraction(400), Fraction(0)),
                    Section("B", Fraction(400), Fraction(1000), Fraction(0)),
                ],
            )

    def test_duplicate_section_id_rejected(self):
        with self.assertRaises(RouteDefinitionError):
            RouteCatalog(
                "x", "L", 1000,
                [
                    Section("A", Fraction(0), Fraction(500), Fraction(0)),
                    Section("A", Fraction(500), Fraction(1000), Fraction(0)),
                ],
            )


# ---------------------------------------------------------------------------
# Point reverse lookup: ambiguity, gaps, half-open
# ---------------------------------------------------------------------------


class TestPointLookup(unittest.TestCase):
    def setUp(self):
        self.cat = route_r1()

    def test_duplicate_label_returns_all_candidates(self):
        r = self.cat.resolve_label_point(350)
        self.assertEqual(r["status"], "ambiguous")
        got = sorted((c["section_id"], c["physical"]) for c in r["candidates"])
        self.assertEqual(
            got, [("P1", Fraction(350)), ("P2", Fraction(450))]
        )
        # Never silently picks the "nearest" single value.
        self.assertEqual(len(r["candidates"]), 2)

    def test_label_gap_is_unresolved(self):
        r = self.cat.resolve_label_point(620)  # P3 starts at label 650
        self.assertEqual(r["status"], "unresolved")
        self.assertEqual(r["candidates"], [])

    def test_label_at_section_right_end_excluded(self):
        # P1 labels [0, 400); label 400 is NOT a P1 point.
        r = self.cat.resolve_label_point(400)
        # 400 is in P2? P2 labels [300,600) -> yes, physical 500.
        self.assertEqual(r["status"], "resolved")
        self.assertEqual(r["candidates"], [{"section_id": "P2", "physical": Fraction(500)}])

    def test_point_on_last_section_right_end_is_unresolved(self):
        # P3 labels [650, 950); 950 is outside the whole route.
        r = self.cat.resolve_label_point(950)
        self.assertEqual(r["status"], "unresolved")

    def test_unique_resolution(self):
        r = self.cat.resolve_label_point(300)
        # 300 is in P2 (unique; P1 ends at 400 so 300 also in P1!)
        # P1 [0,400) includes 300 -> ambiguous actually.
        self.assertEqual(r["status"], "ambiguous")
        r2 = self.cat.resolve_label_point(100)
        self.assertEqual(r2["status"], "resolved")
        self.assertEqual(r2["candidates"], [{"section_id": "P1", "physical": Fraction(100)}])

    def test_section_filter_disambiguates(self):
        # Supply the section explicitly to disambiguate a repeated label.
        r = self.cat.resolve_label_point(350, section_id="P2")
        self.assertEqual(r["status"], "resolved")
        self.assertEqual(r["candidates"], [{"section_id": "P2", "physical": Fraction(450)}])

    def test_section_filter_unknown_is_unresolved(self):
        r = self.cat.resolve_label_point(350, section_id="GHOST")
        self.assertEqual(r["status"], "unresolved")
        self.assertEqual(r["candidates"], [])

    def test_ambiguous_without_section_is_not_resolved(self):
        # No section supplied + two candidates -> ambiguous, never a pick.
        r = self.cat.resolve_label_point(350)
        self.assertEqual(r["status"], "ambiguous")
        self.assertEqual(len(r["candidates"]), 2)


# ---------------------------------------------------------------------------
# Interval conversion, half-open upper bound
# ---------------------------------------------------------------------------


class TestSpanConversion(unittest.TestCase):
    def setUp(self):
        self.cat = route_r1()

    def test_interior_span(self):
        c = self.cat.convert_span("P2", 350, 390)
        self.assertEqual(c["physical"], Interval(Fraction(450), Fraction(490)))

    def test_upper_bound_at_section_end_is_legal(self):
        # P2 labels [300, 600); upper bound 600 == end label is allowed.
        c = self.cat.convert_span("P2", 550, 600)
        self.assertEqual(c["physical"], Interval(Fraction(650), Fraction(700)))

    def test_start_at_section_start_label_is_legal(self):
        c = self.cat.convert_span("P2", 300, 310)
        self.assertEqual(c["physical"], Interval(Fraction(400), Fraction(410)))

    def test_span_beyond_end_rejected(self):
        with self.assertRaises(ValueError):
            self.cat.convert_span("P2", 550, 601)

    def test_span_before_start_rejected(self):
        with self.assertRaises(ValueError):
            self.cat.convert_span("P2", 299, 310)

    def test_non_positive_span_rejected(self):
        with self.assertRaises(ValueError):
            self.cat.convert_span("P2", 350, 350)
        with self.assertRaises(ValueError):
            self.cat.convert_span("P2", 390, 350)

    def test_unknown_section_rejected(self):
        with self.assertRaises(CatalogLookupError):
            self.cat.convert_span("PX", 0, 10)

    def test_rational_slope_one(self):
        cat = RouteCatalog(
            "q", "L", 10,
            [Section("S", Fraction(0), Fraction(10), Fraction(3, 4))],
        )
        c = cat.convert_span("S", Fraction(3, 4), Fraction(7, 4))
        self.assertEqual(c["physical"], Interval(Fraction(0), Fraction(1)))


# ---------------------------------------------------------------------------
# Merge / dedup of overlapping and repeated spans
# ---------------------------------------------------------------------------


class TestMerge(unittest.TestCase):
    def test_overlap_merged_no_double_count(self):
        ivs = [Interval(Fraction(0), Fraction(10)),
               Interval(Fraction(5), Fraction(15))]
        merged = merge_intervals(ivs)
        self.assertEqual(merged, [Interval(Fraction(0), Fraction(15))])
        self.assertEqual(sum(iv.length for iv in merged), Fraction(15))

    def test_identical_repeated_span_dedup(self):
        ivs = [Interval(Fraction(10), Fraction(20))] * 3
        merged = merge_intervals(ivs)
        self.assertEqual(merged, [Interval(Fraction(10), Fraction(20))])
        self.assertEqual(sum(iv.length for iv in merged), Fraction(10))

    def test_touching_spans_merge(self):
        ivs = [Interval(Fraction(0), Fraction(10)),
               Interval(Fraction(10), Fraction(20))]
        self.assertEqual(merge_intervals(ivs), [Interval(Fraction(0), Fraction(20))])

    def test_disjoint_preserved(self):
        ivs = [Interval(Fraction(0), Fraction(5)),
               Interval(Fraction(10), Fraction(20))]
        self.assertEqual(len(merge_intervals(ivs)), 2)

    def test_covered_by_half_open(self):
        merged = [Interval(Fraction(10), Fraction(20))]
        self.assertTrue(covered_by(merged, Fraction(10)))
        self.assertTrue(covered_by(merged, Fraction(19)))
        self.assertFalse(covered_by(merged, Fraction(20)))
        self.assertFalse(covered_by(merged, Fraction(9)))


# ---------------------------------------------------------------------------
# Inspection jobs: coverage, facilities, atomic publish
# ---------------------------------------------------------------------------


class TestInspectionJobs(unittest.TestCase):
    def test_materials_case_r1(self):
        lib = fresh_lib()
        rep = lib.resolve_and_inspect(
            {"job_id": "J", "route_id": "L",
             "spans": [{"section_id": "P2", "label_start": 350, "label_end": 390}]},
            "R1",
        )
        self.assertEqual(rep.catalog_revision, "r1")
        self.assertEqual(rep.merged, (Interval(Fraction(450), Fraction(490)),))
        self.assertEqual([f.asset_id for f in rep.facilities], ["B"])

    def test_revision_changes_real_facilities(self):
        lib = fresh_lib("r2")
        rep = lib.resolve_and_inspect(
            {"job_id": "J", "route_id": "L",
             "spans": [{"section_id": "P2", "label_start": 350, "label_end": 390}]},
            "R2",
        )
        self.assertEqual(rep.merged, (Interval(Fraction(420), Fraction(460)),))
        self.assertEqual([f.asset_id for f in rep.facilities], ["C"])

    def test_old_report_keeps_old_positions(self):
        reg = CatalogRegistry()
        reg.add(route_r1())
        lib = InspectionLibrary(reg, assets_abc())
        r1 = lib.resolve_and_inspect(
            {"job_id": "J", "route_id": "L",
             "spans": [{"section_id": "P2", "label_start": 350, "label_end": 390}]},
            "R1",
        )
        reg.revise_section_label_start("r2", "L", "P2", 330)
        # Old report still explains B at 470.
        view = lib.report_view("R1")
        self.assertEqual(view["facilities"], [{"asset_id": "B", "physical_position": "470/1"}])
        self.assertTrue(view["stale"])

    def test_repeated_spans_dedup(self):
        lib = fresh_lib()
        rep = lib.resolve_and_inspect(
            {"job_id": "D", "route_id": "L",
             "spans": [
                 {"section_id": "P2", "label_start": 350, "label_end": 390},
                 {"section_id": "P2", "label_start": 350, "label_end": 390},
                 {"section_id": "P2", "label_start": 370, "label_end": 400},
             ]},
            "RD",
        )
        # [450,490) and [470,500) -> [450,500)
        self.assertEqual(rep.merged, (Interval(Fraction(450), Fraction(500)),))
        self.assertEqual(rep.covered_length, Fraction(50))

    def test_multi_section_union(self):
        lib = fresh_lib()
        rep = lib.resolve_and_inspect(
            {"job_id": "M", "route_id": "L",
             "spans": [
                 {"section_id": "P1", "label_start": 380, "label_end": 400},
                 {"section_id": "P2", "label_start": 300, "label_end": 320},
             ]},
            "RM",
        )
        # P1 [380,400) -> phys [380,400); P2 [300,320) -> phys [400,420)
        self.assertEqual(rep.merged, (Interval(Fraction(380), Fraction(420)),))
        self.assertEqual(rep.covered_length, Fraction(40))

    def test_unknown_section_aborts_whole_job(self):
        lib = fresh_lib()
        with self.assertRaises(InvalidSpanError):
            lib.resolve_and_inspect(
                {"job_id": "BAD", "route_id": "L",
                 "spans": [
                     {"section_id": "P2", "label_start": 350, "label_end": 390},
                     {"section_id": "NOPE", "label_start": 0, "label_end": 10},
                 ]},
                "RBAD",
            )
        # Nothing published.
        with self.assertRaises(KeyError):
            lib.get_report("RBAD")

    def test_out_of_section_span_aborts_job(self):
        lib = fresh_lib()
        with self.assertRaises(InvalidSpanError):
            lib.resolve_and_inspect(
                {"job_id": "BAD2", "route_id": "L",
                 "spans": [{"section_id": "P2", "label_start": 550, "label_end": 601}]},
                "RBAD2",
            )
        with self.assertRaises(KeyError):
            lib.get_report("RBAD2")

    def test_ambiguous_point_makes_no_facility_hit(self):
        lib = fresh_lib()
        # label 350 has two candidates; but jobs use explicit section + span,
        # so no implicit point lookup can create hits. A job whose span is
        # empty is invalid and must fail (no accidental nearest resolution).
        with self.assertRaises(InvalidSpanError):
            lib.resolve_and_inspect(
                {"job_id": "AMB", "route_id": "L",
                 "spans": [{"section_id": "P2", "label_start": 350, "label_end": 350}]},
                "RAMB",
            )

    def test_no_spans_rejected(self):
        lib = fresh_lib()
        with self.assertRaises(InvalidSpanError):
            lib.resolve_and_inspect(
                {"job_id": "E", "route_id": "L", "spans": []}, "RE"
            )

    def test_facility_identity_fixed_across_revision(self):
        reg = CatalogRegistry()
        reg.add(route_r1())
        lib = InspectionLibrary(reg, assets_abc())
        b_before = [a for a in assets_abc() if a.asset_id == "B"][0]
        reg.revise_section_label_start("r2", "L", "P2", 330)
        b_after = [a for a in assets_abc() if a.asset_id == "B"][0]
        self.assertEqual(b_before.physical_position, b_after.physical_position)
        # And a label-based lookup of B's label differs by revision.
        self.assertEqual(route_r1().section("P2").label_at(Fraction(470)), Fraction(370))


# ---------------------------------------------------------------------------
# Report id semantics: idempotent vs conflict
# ---------------------------------------------------------------------------


class TestReportIds(unittest.TestCase):
    def test_same_content_idempotent(self):
        lib = fresh_lib()
        job = {"job_id": "J", "route_id": "L",
               "spans": [{"section_id": "P2", "label_start": 350, "label_end": 390}]}
        r1 = lib.resolve_and_inspect(job, "R")
        r2 = lib.resolve_and_inspect(job, "R")
        self.assertEqual(r1.fingerprint(), r2.fingerprint())

    def test_different_content_same_id_rejected(self):
        lib = fresh_lib()
        lib.resolve_and_inspect(
            {"job_id": "J", "route_id": "L",
             "spans": [{"section_id": "P2", "label_start": 350, "label_end": 390}]},
            "R",
        )
        with self.assertRaises(ReportConflictError):
            lib.resolve_and_inspect(
                {"job_id": "J", "route_id": "L",
                 "spans": [{"section_id": "P2", "label_start": 360, "label_end": 390}]},
                "R",
            )

    def test_same_report_after_revision_is_conflict(self):
        reg = CatalogRegistry()
        reg.add(route_r1())
        lib = InspectionLibrary(reg, assets_abc())
        job = {"job_id": "J", "route_id": "L",
               "spans": [{"section_id": "P2", "label_start": 350, "label_end": 390}]}
        lib.resolve_and_inspect(job, "R")
        reg.revise_section_label_start("r2", "L", "P2", 330)
        # Reusing R with new physical coverage must be rejected.
        with self.assertRaises(ReportConflictError):
            lib.resolve_and_inspect(job, "R")


# ---------------------------------------------------------------------------
# SQLite persistence round-trips
# ---------------------------------------------------------------------------


class TestSqlitePersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = sqlite3.connect(":memory:")
        self.tmp.row_factory = sqlite3.Row
        self.tmp.execute("PRAGMA foreign_keys = ON")
        self.tmp.executescript(chainage_catalog.SCHEMA)
        ensure_inspection_schema(self.tmp)

    def tearDown(self):
        self.tmp.close()

    def test_catalog_roundtrip_and_registry_order(self):
        cat = route_r1()
        persist_catalog(self.tmp, cat)
        reg = load_catalog_registry(self.tmp)
        self.assertEqual(reg.current().revision_id, "r1")
        self.assertEqual(reg.current().sections[1].label_start, Fraction(300))

    def test_report_roundtrip(self):
        lib = fresh_lib()
        rep = lib.resolve_and_inspect(
            {"job_id": "J", "route_id": "L",
             "spans": [{"section_id": "P2", "label_start": 350, "label_end": 390}]},
            "R1",
        )
        persist_report(self.tmp, rep)
        back = load_report(self.tmp, "R1")
        self.assertEqual(back.fingerprint(), rep.fingerprint())
        self.assertEqual(back.merged, rep.merged)
        self.assertEqual([f.asset_id for f in back.facilities], ["B"])

    def test_report_id_content_conflict_in_sqlite(self):
        lib = fresh_lib()
        rep = lib.resolve_and_inspect(
            {"job_id": "J", "route_id": "L",
             "spans": [{"section_id": "P2", "label_start": 350, "label_end": 390}]},
            "R1",
        )
        persist_report(self.tmp, rep)
        other = lib.resolve_and_inspect(
            {"job_id": "J", "route_id": "L",
             "spans": [{"section_id": "P2", "label_start": 360, "label_end": 390}]},
            "R2",
        )
        other_dict = other.to_dict()
        other_dict["report_id"] = "R1"  # force the same id
        from inspection_library import Report

        forged = Report(**{**other.__dict__, "report_id": "R1"})
        with self.assertRaises(ReportConflictError):
            persist_report(self.tmp, forged)

    def test_persist_same_report_twice_ok(self):
        lib = fresh_lib()
        rep = lib.resolve_and_inspect(
            {"job_id": "J", "route_id": "L",
             "spans": [{"section_id": "P2", "label_start": 350, "label_end": 390}]},
            "R1",
        )
        persist_report(self.tmp, rep)
        persist_report(self.tmp, rep)  # idempotent


# ---------------------------------------------------------------------------
# Route revision window
# ---------------------------------------------------------------------------


class TestRevisionWindow(unittest.TestCase):
    def test_only_label_start_changes(self):
        reg = CatalogRegistry()
        reg.add(route_r1())
        r2 = reg.revise_section_label_start("r2", "L", "P2", 330)
        old = reg.get("r1")
        for so, sn in zip(old.sections, r2.sections):
            self.assertEqual(so.physical_start, sn.physical_start)
            self.assertEqual(so.physical_end, sn.physical_end)
            if so.section_id == "P2":
                self.assertEqual(sn.label_start, Fraction(330))
            else:
                self.assertEqual(so.label_start, sn.label_start)

    def test_unknown_section_revision_fails(self):
        reg = CatalogRegistry()
        reg.add(route_r1())
        with self.assertRaises(CatalogLookupError):
            reg.revise_section_label_start("r2", "L", "NOPE", 0)

    def test_duplicate_revision_rejected(self):
        reg = CatalogRegistry()
        reg.add(route_r1())
        with self.assertRaises(RouteDefinitionError):
            reg.add(route_r1())


if __name__ == "__main__":
    unittest.main()
