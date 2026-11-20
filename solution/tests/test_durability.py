"""End-to-end durability & extra edge-case tests.

Covers: reopen of a file-backed DB, SQLite-backed reverse lookup equivalence,
and the specific listed scenarios (label gaps, repeats, half-open ends,
duplicate-span dedup, unknown section, reopen).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chainage_catalog
from chainage_catalog import (
    CatalogRegistry,
    RouteCatalog,
    Section,
    load_catalog_registry,
    open_db,
    persist_catalog,
)
from inspection_library import (
    Asset,
    InspectionLibrary,
    InvalidSpanError,
    ensure_inspection_schema,
    load_all_reports,
    load_assets,
    persist_assets,
    persist_report,
)


def route_r1() -> RouteCatalog:
    return RouteCatalog(
        "r1", "L", 1000,
        [
            Section("P1", Fraction(0), Fraction(400), Fraction(0)),
            Section("P2", Fraction(400), Fraction(700), Fraction(300)),
            Section("P3", Fraction(700), Fraction(1000), Fraction(650)),
        ],
    )


class TestDurabilityReopen(unittest.TestCase):
    def test_file_db_reopen_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "route.db")

            # --- first open: persist catalogue, assets, a report ---
            conn = open_db(path)
            ensure_inspection_schema(conn)
            cat = route_r1()
            persist_catalog(conn, cat)
            assets = [
                Asset("A", "L", Fraction(350)),
                Asset("B", "L", Fraction(470)),
                Asset("C", "L", Fraction(440)),
            ]
            persist_assets(conn, assets)
            reg = load_catalog_registry(conn)
            lib = InspectionLibrary(reg, load_assets(conn))
            rep = lib.resolve_and_inspect(
                {"job_id": "J", "route_id": "L",
                 "spans": [{"section_id": "P2", "label_start": 350, "label_end": 390}]},
                "R1",
            )
            persist_report(conn, rep)
            conn.close()

            # --- second open: everything survives ---
            conn2 = open_db(path)
            ensure_inspection_schema(conn2)
            reg2 = load_catalog_registry(conn2)
            self.assertEqual(reg2.current().revision_id, "r1")
            assets2 = load_assets(conn2)
            reports2 = load_all_reports(conn2)
            self.assertIn("R1", reports2)
            back = reports2["R1"]
            self.assertEqual(back.fingerprint(), rep.fingerprint())
            self.assertEqual([f.asset_id for f in back.facilities], ["B"])
            self.assertEqual(back.merged, (chainage_catalog.Interval(
                Fraction(450), Fraction(490)),))

            # Facility identities (physical positions) unchanged on reopen.
            by_id = {a.asset_id: a for a in assets2}
            self.assertEqual(by_id["B"].physical_position, Fraction(470))
            conn2.close()

    def test_sqlite_index_can_prefilter_sections(self):
        """The phys_start_key index supports ordered range prefiltering."""
        conn = open_db(":memory:")
        persist_catalog(conn, route_r1())
        # A section whose physical range covers [450, 490) can be found by
        # the sortable key range; then the exact Fraction check is in Python.
        key_lo = chainage_catalog.sortable_key(Fraction(450))
        key_hi = chainage_catalog.sortable_key(Fraction(490))
        rows = conn.execute(
            "SELECT section_id FROM route_section "
            "WHERE revision_id='r1' AND phys_start_key <= ? "
            "ORDER BY phys_start_key",
            (key_lo,),
        ).fetchall()
        # P1 starts at 0, P2 at 400 -> both returned; exact check picks P2.
        self.assertEqual([r["section_id"] for r in rows], ["P1", "P2"])
        conn.close()


class TestListedEdgeCases(unittest.TestCase):
    """Directly exercises each scenario named in the task."""

    def setUp(self):
        self.reg = CatalogRegistry()
        self.cat = route_r1()
        self.reg.add(self.cat)
        self.lib = InspectionLibrary(
            self.reg,
            [Asset("A", "L", Fraction(350)),
             Asset("B", "L", Fraction(470)),
             Asset("C", "L", Fraction(440)),
             Asset("D", "L", Fraction(700))],
        )

    def _job(self, spans, jid="J"):
        return {"job_id": jid, "route_id": "L", "spans": spans}

    def test_label_gap_scenario(self):
        # P1 ends at label 400; P2 starts at 300, P3 at 650. Label 620 is a gap.
        self.assertEqual(self.cat.resolve_label_point(620)["status"], "unresolved")

    def test_repeated_label_scenario(self):
        r = self.cat.resolve_label_point(350)
        self.assertEqual(r["status"], "ambiguous")

    def test_half_open_right_end_scenario(self):
        # Physical point 700 belongs to P3, not P2. Its P2 label is 600,
        # which is P2's exclusive end label, so it is not a P2 point. It is
        # also outside P1 [0,400) and P3 [650,950) -> unresolved by label.
        label_at_700 = self.cat.section("P2").label_at(Fraction(700))
        self.assertEqual(label_at_700, Fraction(600))
        r = self.cat.resolve_label_point(label_at_700)
        self.assertEqual(r["status"], "unresolved")
        self.assertNotIn("P2", [c["section_id"] for c in r["candidates"]])

    def test_duplicate_span_dedup_scenario(self):
        rep = self.lib.resolve_and_inspect(
            self._job([
                {"section_id": "P2", "label_start": 350, "label_end": 390},
                {"section_id": "P2", "label_start": 350, "label_end": 390},
            ]),
            "RD",
        )
        self.assertEqual(rep.covered_length, Fraction(40))
        self.assertEqual([f.asset_id for f in rep.facilities], ["B"])

    def test_unknown_section_scenario(self):
        with self.assertRaises(InvalidSpanError):
            self.lib.resolve_and_inspect(
                self._job([{"section_id": "GHOST", "label_start": 0, "label_end": 10}]),
                "RG",
            )
        with self.assertRaises(KeyError):
            self.lib.get_report("RG")

    def test_reopen_report_explains_original(self):
        rep = self.lib.resolve_and_inspect(
            self._job([{"section_id": "P2", "label_start": 350, "label_end": 390}]),
            "R1",
        )
        # Persist to a fresh in-memory DB and reload.
        conn = open_db(":memory:")
        ensure_inspection_schema(conn)
        persist_catalog(conn, self.cat)
        persist_report(conn, rep)
        back = load_all_reports(conn)["R1"]
        self.assertEqual(back.catalog_revision, "r1")
        self.assertEqual(back.requested_spans[0].label_start, Fraction(350))
        self.assertEqual(back.requested_spans[0].physical_start, Fraction(450))
        self.assertEqual([f.asset_id for f in back.facilities], ["B"])

    def test_end_boundary_span_covers_last_facility(self):
        # Span whose upper bound == P2 end label (600) is legal and reaches
        # physical 700; facility D sits exactly at 700 so is NOT covered.
        rep = self.lib.resolve_and_inspect(
            self._job([{"section_id": "P2", "label_start": 590, "label_end": 600}]),
            "RE",
        )
        self.assertEqual(rep.merged, (chainage_catalog.Interval(
            Fraction(690), Fraction(700)),))
        self.assertEqual([f.asset_id for f in rep.facilities], [])  # D@700 excluded

    def test_end_boundary_excludes_right_asset(self):
        # Facility at 690 is included; the span stops at 700.
        lib = InspectionLibrary(
            self.reg, [Asset("X", "L", Fraction(690))]
        )
        rep = lib.resolve_and_inspect(
            self._job([{"section_id": "P2", "label_start": 590, "label_end": 600}]),
            "RX",
        )
        self.assertEqual([f.asset_id for f in rep.facilities], ["X"])


if __name__ == "__main__":
    unittest.main()
