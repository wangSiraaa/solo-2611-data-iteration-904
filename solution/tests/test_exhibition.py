"""Tests for temporary-exhibition rosters and settlement.

Run:  python3 -m unittest discover -s solution/tests -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chainage_catalog import CatalogRegistry, RouteCatalog, Section  # noqa: E402
from inspection_library import Asset, AssetsIndex  # noqa: E402

from exhibition_roster import (  # noqa: E402
    RosterBook,
    RosterError,
    RosterQueryError,
    RosterRevisionError,
    classify_at,
    ensure_roster_schema,
    facility_timeline,
    load_roster_book,
    make_shift,
    persist_roster_book,
)
from exhibition_settlement import (  # noqa: E402
    MAX_SESSIONS,
    SettlementError,
    SettlementPlanner,
    TicketError,
    ensure_settlement_schema,
    facility_usable_segments,
    invalid_witnesses,
    load_contract,
    make_session,
    make_ticket,
    persist_contract,
    sign_contract,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def route_r1() -> RouteCatalog:
    return RouteCatalog(
        "r1", "L", 1000,
        [
            Section("P1", Fraction(0), Fraction(400), Fraction(0)),
            Section("P2", Fraction(400), Fraction(700), Fraction(300)),
            Section("P3", Fraction(700), Fraction(1000), Fraction(650)),
        ],
    )


EXH = "EXH@P2"   # physical 450 (P2, label 350)
ALT = "ALT@P1"   # physical 350 (P1, label 350)  -- same label, other section


def assets() -> AssetsIndex:
    return AssetsIndex([Asset(EXH, "L", Fraction(450)), Asset(ALT, "L", Fraction(350))])


SPANS = [
    {"section_id": "P2", "label_start": 340, "label_end": 360},
    {"section_id": "P1", "label_start": 340, "label_end": 360},
]


def sh(sid, fac, person, org, start, end):
    return make_shift(sid, fac, person, org, start, end)


def fresh(registry=None) -> RosterBook:
    reg = registry or CatalogRegistry()
    if not reg.all_revisions():
        reg.add(route_r1())
    return RosterBook("BK", "L", reg, assets())


def sessions_xyz(facility=EXH):
    return (
        make_session("X", facility, 0, 8, "甲", "乙", 7),
        make_session("Y", facility, 0, 4, "甲", "乙", 4),
        make_session("Z", facility, 4, 8, "甲", "乙", 4),
    )


def plan_ids(book, facility=EXH):
    x, y, z = sessions_xyz(facility)
    ticket = make_ticket("T", facility, [x, y, z])
    return SettlementPlanner(book).plan(ticket)


# ---------------------------------------------------------------------------
# Roster: construction, validation, revision semantics
# ---------------------------------------------------------------------------


class TestRosterConstruction(unittest.TestCase):
    def test_shift_requires_positive_half_open(self):
        with self.assertRaises(RosterError):
            make_shift("s", EXH, "p", "甲", 4, 4)
        with self.assertRaises(RosterError):
            make_shift("s", EXH, "p", "甲", 5, 4)

    def test_shift_at_facility_not_covered_is_rejected(self):
        book = fresh()
        with self.assertRaises(RosterRevisionError):
            book.add_replace(
                "v1", "r1",
                [{"section_id": "P2", "label_start": 340, "label_end": 360}],
                [sh("s", ALT, "p", "甲", 0, 8)],  # ALT not covered by this span
            )

    def test_empty_spans_rejected(self):
        book = fresh()
        with self.assertRaises(RosterRevisionError):
            book.add_replace("v1", "r1", [], [])

    def test_invalid_span_rejected(self):
        book = fresh()
        with self.assertRaises(RosterRevisionError):
            book.add_replace(
                "v1", "r1",
                [{"section_id": "P2", "label_start": 550, "label_end": 601}],
                [],
            )

    def test_wrong_route_rejected(self):
        book = fresh()
        with self.assertRaises(RosterRevisionError):
            book.add_replace(
                "v1", "nope",
                [{"section_id": "P2", "label_start": 340, "label_end": 360}],
                [],
            )

    def test_duplicate_revision_rejected(self):
        book = fresh()
        book.add_replace("v1", "r1", SPANS, [sh("s", EXH, "p", "甲", 0, 8)])
        with self.assertRaises(RosterRevisionError):
            book.add_replace("v1", "r1", SPANS, [])

    def test_replacement_is_full_snapshot(self):
        book = fresh()
        book.add_replace("v1", "r1", SPANS, [sh("s1", EXH, "p", "甲", 0, 8)])
        book.add_replace("v2", "r1", SPANS, [sh("s2", EXH, "q", "乙", 0, 8)])
        # v2 replaces v1 wholesale: p is gone, q remains.
        self.assertEqual(book.get("v1").shifts[0].person_id, "p")
        self.assertEqual(
            [s.person_id for s in book.current().shifts], ["q"]
        )

    def test_revoke_clears_presence(self):
        book = fresh()
        book.add_replace("v1", "r1", SPANS, [sh("s", EXH, "p", "甲", 0, 8)])
        rev = book.add_revoke("v2")
        self.assertTrue(rev.revoked)
        self.assertEqual(rev.shifts, ())
        self.assertEqual(rev.covered_facilities, ())

    def test_unknown_revision_lookup_raises(self):
        book = fresh()
        with self.assertRaises(RosterQueryError):
            book.get("ghost")

    def test_same_label_other_section_is_other_facility(self):
        # P1 label 350 -> physical 350 (ALT); P2 label 350 -> physical 450 (EXH).
        cat = route_r1()
        r = cat.resolve_label_point(350)
        self.assertEqual(r["status"], "ambiguous")
        phys = {c["section_id"]: c["physical"] for c in r["candidates"]}
        self.assertEqual(phys["P1"], Fraction(350))
        self.assertEqual(phys["P2"], Fraction(450))


# ---------------------------------------------------------------------------
# Attribution: disputes, adjacency, half-open
# ---------------------------------------------------------------------------


class TestAttribution(unittest.TestCase):
    def test_cross_org_overlap_excluded_for_both(self):
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [
                sh("a", EXH, "陈", "甲", 0, 8),
                sh("b", EXH, "陈", "乙", 2, 4),
                sh("c", EXH, "周", "乙", 0, 8),
            ],
        )
        rev = book.get("v1")
        # At t=1: 陈 usable for 甲 only.
        staff, disputes = classify_at(rev.shifts, Fraction(1))
        self.assertEqual(staff, {"甲": ("陈",), "乙": ("周",)})
        self.assertEqual(disputes, ())
        # At t=3: 陈 disputed, excluded for both.
        staff, disputes = classify_at(rev.shifts, Fraction(3))
        self.assertEqual(staff, {"乙": ("周",)})
        self.assertEqual(disputes[0].person_id, "陈")
        self.assertEqual(disputes[0].orgs, ("乙", "甲"))
        # At t=4 the 乙 shift ends (half-open): 陈 usable for 甲 again.
        staff, _ = classify_at(rev.shifts, Fraction(4))
        self.assertIn("甲", staff)
        self.assertIn("陈", staff["甲"])

    def test_half_open_dispute_boundary(self):
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [
                sh("a", EXH, "陈", "甲", 0, 8),
                sh("b", EXH, "陈", "乙", 2, 4),
            ],
        )
        rev = book.get("v1")
        # 乙 on [2,4): disputed at t=2 and t=3.999, not at t=4.
        self.assertEqual(len(classify_at(rev.shifts, Fraction(2))[1]), 1)
        self.assertEqual(len(classify_at(rev.shifts, Fraction(4))[1]), 0)

    def test_adjacent_handover_keeps_coverage(self):
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [
                sh("a1", EXH, "陈", "甲", 0, 4),
                sh("a2", EXH, "李", "甲", 4, 8),
                sh("b", EXH, "周", "乙", 0, 8),
            ],
        )
        ticket = make_ticket("T", EXH, [make_session("F", EXH, 0, 8, "甲", "乙", 9)])
        plan = SettlementPlanner(book).plan(ticket)
        self.assertEqual(plan.selected_ids(), ["F"])
        a_segs = [s for s in plan.witnesses() if s.org_id == "甲"]
        self.assertEqual(
            [(s.person_id, s.start, s.end) for s in a_segs],
            [("陈", Fraction(0), Fraction(4)), ("李", Fraction(4), Fraction(8))],
        )

    def test_gap_in_coverage_breaks_window(self):
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [
                sh("a1", EXH, "陈", "甲", 0, 3),
                sh("a2", EXH, "陈", "甲", 5, 8),   # gap [3,5)
                sh("b", EXH, "周", "乙", 0, 8),
            ],
        )
        plan = plan_ids(book)
        self.assertEqual(plan.selected_ids(), [])
        self.assertEqual(sorted(plan.unsigned), ["X", "Y", "Z"])

    def test_relocation_to_same_label_other_section_breaks_facility(self):
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [
                sh("a", EXH, "陈", "甲", 0, 8),
                sh("b", EXH, "周", "乙", 0, 8),
            ],
        )
        self.assertEqual(plan_ids(book).selected_ids(), ["Y", "Z"])
        book.add_replace(
            "v2", "r1", SPANS,
            [
                sh("a1", EXH, "陈", "甲", 0, 3),
                sh("a2", ALT, "陈", "甲", 3, 5),   # moved, same facility label
                sh("a3", EXH, "陈", "甲", 5, 8),
                sh("b", EXH, "周", "乙", 0, 8),
            ],
        )
        plan = plan_ids(book)
        self.assertEqual(plan.selected_ids(), [])
        self.assertEqual(sorted(plan.unsigned), ["X", "Y", "Z"])
        # And ALT sees 陈 there for [3,5) -- it really moved.
        alt_segs = facility_usable_segments(book.current(), ALT)
        self.assertEqual(
            [(u.person_id, u.start, u.end) for u in alt_segs],
            [("陈", Fraction(3), Fraction(5))],
        )


# ---------------------------------------------------------------------------
# Settlement: session validation, optimality, capacity
# ---------------------------------------------------------------------------


class TestSessionValidation(unittest.TestCase):
    def test_two_distinct_orgs_required(self):
        with self.assertRaises(TicketError):
            make_session("S", EXH, 0, 4, "甲", "甲", 1)

    def test_window_must_be_positive(self):
        with self.assertRaises(TicketError):
            make_session("S", EXH, 4, 4, "甲", "乙", 1)

    def test_negative_value_rejected(self):
        with self.assertRaises(TicketError):
            make_session("S", EXH, 0, 4, "甲", "乙", -1)

    def test_ticket_session_facility_mismatch(self):
        s = make_session("S", ALT, 0, 4, "甲", "乙", 1)
        with self.assertRaises(TicketError):
            make_ticket("T", EXH, [s])

    def test_duplicate_session_id(self):
        a = make_session("S", EXH, 0, 4, "甲", "乙", 1)
        b = make_session("S", EXH, 1, 2, "甲", "乙", 1)
        with self.assertRaises(TicketError):
            make_ticket("T", EXH, [a, b])

    def test_at_most_20_sessions(self):
        many = [make_session(f"S{i}", EXH, i, i + 1, "甲", "乙", 1)
                for i in range(MAX_SESSIONS + 1)]
        with self.assertRaises(TicketError):
            make_ticket("T", EXH, many)
        make_ticket("T", EXH, many[:MAX_SESSIONS])  # 20 is fine


class TestOptimality(unittest.TestCase):
    def test_baseline_prefers_y_plus_z(self):
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [sh("a", EXH, "陈", "甲", 0, 8), sh("b", EXH, "周", "乙", 0, 8)],
        )
        plan = plan_ids(book)
        self.assertEqual(plan.total_value, Fraction(8))
        self.assertEqual(plan.selected_ids(), ["Y", "Z"])
        self.assertEqual(sorted(plan.signable), ["X", "Y", "Z"])

    def test_reinforcement_reaches_fifteen(self):
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [
                sh("a", EXH, "陈", "甲", 0, 8),
                sh("a2", EXH, "李", "甲", 0, 8),
                sh("b", EXH, "周", "乙", 0, 8),
                sh("b2", EXH, "赵", "乙", 0, 8),
            ],
        )
        plan = plan_ids(book)
        self.assertEqual(plan.total_value, Fraction(15))
        self.assertEqual(plan.selected_ids(), ["X", "Y", "Z"])

    def test_single_person_cannot_witness_two_overlapping(self):
        # 甲 shared between two fully-overlapping sessions -> only one signs.
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [
                sh("a", EXH, "陈", "甲", 0, 8),
                sh("b", EXH, "周", "乙", 0, 8),
                sh("c", EXH, "吴", "丙", 0, 8),
            ],
        )
        p = make_session("P", EXH, 0, 8, "甲", "乙", 6)
        q = make_session("Q", EXH, 0, 8, "甲", "丙", 6)
        ticket = make_ticket("T", EXH, [p, q])
        plan = SettlementPlanner(book).plan(ticket)
        self.assertEqual(len(plan.selected_ids()), 1)
        self.assertEqual(plan.total_value, Fraction(6))

    def test_capacity_lets_disjoint_windows_share_person(self):
        # Same two people cover two non-overlapping sessions.
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [sh("a", EXH, "陈", "甲", 0, 8), sh("b", EXH, "周", "乙", 0, 8)],
        )
        p = make_session("P", EXH, 0, 4, "甲", "乙", 5)
        q = make_session("Q", EXH, 4, 8, "甲", "乙", 5)
        ticket = make_ticket("T", EXH, [p, q])
        plan = SettlementPlanner(book).plan(ticket)
        self.assertEqual(plan.selected_ids(), ["P", "Q"])
        self.assertEqual(plan.total_value, Fraction(10))

    def test_selection_is_exact_with_disjoint_resources(self):
        # 4 sessions with independent staff -> take all.
        book = fresh()
        shifts = []
        for i, (org, person) in enumerate(
            [("甲", "a"), ("乙", "b"), ("甲", "c"), ("乙", "d")]
        ):
            shifts.append(sh(f"s{i}", EXH, person, org, 0, 8))
        book.add_replace("v1", "r1", SPANS, shifts)
        s1 = make_session("S1", EXH, 0, 8, "甲", "乙", 3)
        s2 = make_session("S2", EXH, 0, 8, "甲", "乙", 4)
        plan = SettlementPlanner(book).plan(make_ticket("T", EXH, [s1, s2]))
        # Two disjoint 甲 and two disjoint 乙 -> both fit.
        self.assertEqual(plan.total_value, Fraction(7))
        self.assertEqual(plan.selected_ids(), ["S1", "S2"])

    def test_tie_yields_one_optimal(self):
        # Two equal-value incompatible sessions: exactly one, value 5.
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [sh("a", EXH, "陈", "甲", 0, 8),
             sh("b", EXH, "周", "乙", 0, 8),
             sh("c", EXH, "吴", "丙", 0, 8)],
        )
        p = make_session("P", EXH, 0, 8, "甲", "乙", 5)
        q = make_session("Q", EXH, 0, 8, "甲", "丙", 5)
        plan = SettlementPlanner(book).plan(make_ticket("T", EXH, [p, q]))
        self.assertEqual(plan.total_value, Fraction(5))
        self.assertEqual(len(plan.selected_ids()), 1)

    def test_unsigned_when_org_missing(self):
        book = fresh()
        book.add_replace("v1", "r1", SPANS, [sh("a", EXH, "陈", "甲", 0, 8)])
        plan = plan_ids(book)
        self.assertEqual(plan.selected_ids(), [])
        self.assertEqual(sorted(plan.unsigned), ["X", "Y", "Z"])

    def test_render_has_person_and_shift_per_segment(self):
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [sh("a", EXH, "陈", "甲", 0, 8), sh("b", EXH, "周", "乙", 0, 8)],
        )
        plan = plan_ids(book)
        self.assertTrue(plan.selected)
        for seg in plan.witnesses():
            self.assertTrue(seg.person_id)
            self.assertTrue(seg.shift_id)
            self.assertEqual(seg.roster_revision_id, "v1")
            self.assertEqual(seg.catalog_revision, "r1")
            self.assertLess(seg.start, seg.end)


# ---------------------------------------------------------------------------
# Frozen contracts and invalidation
# ---------------------------------------------------------------------------


class TestContracts(unittest.TestCase):
    def _baseline_book(self):
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [sh("a", EXH, "陈", "甲", 0, 8), sh("b", EXH, "周", "乙", 0, 8)],
        )
        return book

    def test_signature_freezes_revisions_and_people(self):
        book = self._baseline_book()
        contract = sign_contract("CT", plan_ids(book))
        self.assertEqual(contract.roster_revision_id, "v1")
        self.assertEqual(contract.catalog_revision, "r1")
        people = {(s.session_id, s.person_id) for s in contract.sessions[0].segments}
        self.assertTrue(people)

    def test_old_contract_still_names_people_after_relocation(self):
        book = self._baseline_book()
        contract = sign_contract("CT", plan_ids(book))
        before = [
            (p.session.session_id, seg.org_id, seg.person_id)
            for p in contract.sessions for seg in p.segments
        ]
        book.add_replace(
            "v2", "r1", SPANS,
            [
                sh("a1", EXH, "陈", "甲", 0, 3),
                sh("a2", ALT, "陈", "甲", 3, 5),
                sh("a3", EXH, "陈", "甲", 5, 8),
                sh("b", EXH, "周", "乙", 0, 8),
            ],
        )
        # Contract object is immutable, still shows the original people.
        after = [
            (p.session.session_id, seg.org_id, seg.person_id)
            for p in contract.sessions for seg in p.segments
        ]
        self.assertEqual(before, after)
        bad = invalid_witnesses(contract, book.current())
        self.assertEqual(
            sorted((w.segment.session_id, w.segment.person_id) for w in bad),
            [("Y", "陈"), ("Z", "陈")],
        )
        self.assertTrue(all(w.reason == "presence_gap" for w in bad))

    def test_replanning_after_relocation_yields_empty(self):
        book = self._baseline_book()
        sign_contract("CT", plan_ids(book))
        book.add_replace(
            "v2", "r1", SPANS,
            [
                sh("a1", EXH, "陈", "甲", 0, 3),
                sh("a2", ALT, "陈", "甲", 3, 5),
                sh("a3", EXH, "陈", "甲", 5, 8),
                sh("b", EXH, "周", "乙", 0, 8),
            ],
        )
        self.assertEqual(plan_ids(book).selected_ids(), [])

    def test_revocation_invalidates_every_witness(self):
        book = self._baseline_book()
        contract = sign_contract("CT", plan_ids(book))
        book.add_revoke("v2")
        bad = invalid_witnesses(contract, book.current())
        self.assertEqual(len(bad), len(contract.sessions) * 2)
        self.assertTrue(all(w.reason == "roster_revoked" for w in bad))
        # A revoked roster selects nothing.
        self.assertEqual(plan_ids(book).selected_ids(), [])

    def test_dispute_invalidates_old_witness(self):
        book = self._baseline_book()
        contract = sign_contract("CT", plan_ids(book))
        # 陈 now also recorded by 乙 over [1,2) -> disputed there.
        book.add_replace(
            "v2", "r1", SPANS,
            [
                sh("a", EXH, "陈", "甲", 0, 8),
                sh("a2", EXH, "陈", "乙", 1, 2),
                sh("b", EXH, "周", "乙", 0, 8),
            ],
        )
        bad = invalid_witnesses(contract, book.current())
        self.assertTrue(bad)
        reasons = {w.reason for w in bad}
        self.assertIn("cross_org_dispute", reasons)

    def test_no_invalid_when_contract_still_supported(self):
        book = self._baseline_book()
        contract = sign_contract("CT", plan_ids(book))
        self.assertEqual(invalid_witnesses(contract, book.current()), [])


# ---------------------------------------------------------------------------
# Timeline / on-site lookup
# ---------------------------------------------------------------------------


class TestTimeline(unittest.TestCase):
    def test_segments_cover_dispute_windows(self):
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [
                sh("a", EXH, "陈", "甲", 0, 8),
                sh("a2", EXH, "陈", "乙", 2, 4),
                sh("b", EXH, "周", "乙", 0, 8),
            ],
        )
        view = book.lookup("v1", EXH)
        segs = [(Fraction(s["start"]), Fraction(s["end"]), s["staff"], s["disputed"])
                for s in view["segments"]]
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[1][3][0]["person_id"], "陈")
        self.assertEqual(segs[0][3], [])
        self.assertEqual(segs[2][3], [])

    def test_lookup_reports_uncovered_facility(self):
        book = fresh()
        book.add_replace(
            "v1", "r1",
            [{"section_id": "P2", "label_start": 340, "label_end": 360}],
            [sh("a", EXH, "陈", "甲", 0, 8)],
        )
        view = book.lookup("v1", ALT)
        self.assertFalse(view["covered"])
        self.assertEqual(view["segments"], [])


# ---------------------------------------------------------------------------
# Persistence round-trips
# ---------------------------------------------------------------------------


class TestPersistence(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_roster_schema(conn)
        ensure_settlement_schema(conn)
        return conn

    def test_roster_book_roundtrip(self):
        reg = CatalogRegistry()
        reg.add(route_r1())
        book = RosterBook("BK", "L", reg, assets())
        book.add_replace(
            "v1", "r1", SPANS,
            [sh("a", EXH, "陈", "甲", 0, 8), sh("b", EXH, "周", "乙", 0, 8)],
        )
        book.add_replace(
            "v2", "r1", SPANS,
            [sh("a", EXH, "陈", "甲", 0, 4), sh("a2", EXH, "李", "甲", 4, 8),
             sh("b", EXH, "周", "乙", 0, 8)],
        )
        conn = self._conn()
        persist_roster_book(conn, book)
        back = load_roster_book(conn, "BK", reg, assets())
        self.assertEqual([r.revision_id for r in back.all_revisions()], ["v1", "v2"])
        self.assertEqual(back.current().revision_id, "v2")
        self.assertEqual(
            [(s.person_id, s.start, s.end) for s in back.current().shifts],
            [("陈", Fraction(0), Fraction(4)),
             ("周", Fraction(0), Fraction(8)),
             ("李", Fraction(4), Fraction(8))],
        )
        self.assertEqual(back.current().spans[0].physical_start, Fraction(440))
        conn.close()

    def test_contract_roundtrip_and_conflict(self):
        book = fresh()
        book.add_replace(
            "v1", "r1", SPANS,
            [sh("a", EXH, "陈", "甲", 0, 8), sh("b", EXH, "周", "乙", 0, 8)],
        )
        contract = sign_contract("CT", plan_ids(book))
        conn = self._conn()
        persist_contract(conn, contract)
        persist_contract(conn, contract)  # idempotent
        back = load_contract(conn, "CT")
        self.assertEqual(back.to_dict(), contract.to_dict())
        self.assertEqual(back.total_value, Fraction(8))
        self.assertEqual([p.session.session_id for p in back.sessions], ["Y", "Z"])
        conn.close()

    def test_roster_revision_persist_is_idempotent(self):
        book = fresh()
        book.add_replace("v1", "r1", SPANS, [sh("a", EXH, "陈", "甲", 0, 8)])
        conn = self._conn()
        persist_roster_book(conn, book)
        persist_roster_book(conn, book)
        n = conn.execute("SELECT COUNT(*) FROM roster_shift").fetchone()[0]
        self.assertEqual(n, 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
