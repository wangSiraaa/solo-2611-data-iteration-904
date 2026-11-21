"""Reproducible demonstration of temporary-exhibition settlement.

Runs the concrete scenario from the task on the materials/730 route, showing
three successive changes:

  (0) baseline   -- one 甲 and one 乙 witness cover [0,8); the three sessions
                    X=[0,8)/7, Y=[0,4)/4, Z=[4,8)/4; optimal single ticket is
                    Y+Z = 8 (X is individually signable but loses out).
  (1) reinforcement -- two more 甲乙 witnesses also cover [0,8); the same
                    ticket now reaches X+Y+Z = 15.
  (2) relocation  -- back to one 甲/one 乙, 小陈's actual position on [3,5) is
                    moved to the *other section carrying the same label*; all
                    three sessions lose full-window witnessing, and the already
                    signed contract still shows the people it was signed with.

It also shows the on-site lookup with the cross-org dispute source, adjacent
handover, and two staffing styles (dense overlap vs. all-day staggered).

Run:  python3 solution/exhibition_demo.py
"""

from __future__ import annotations

import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chainage_catalog import CatalogRegistry, RouteCatalog  # noqa: E402
from inspection_library import Asset, AssetsIndex  # noqa: E402

from exhibition_roster import (  # noqa: E402
    RosterBook,
    facility_timeline,
)
from exhibition_settlement import (  # noqa: E402
    SettlementPlanner,
    invalid_witnesses,
    make_session,
    make_ticket,
    sign_contract,
)


# ---------------------------------------------------------------------------
# Fixtures from materials/730 (r1)
# ---------------------------------------------------------------------------


def route_r1() -> RouteCatalog:
    from chainage_catalog import Section

    return RouteCatalog(
        "r1", "L", 1000,
        [
            Section("P1", Fraction(0), Fraction(400), Fraction(0)),      # labels [0,400)
            Section("P2", Fraction(400), Fraction(700), Fraction(300)),  # labels [300,600)
            Section("P3", Fraction(700), Fraction(1000), Fraction(650)), # labels [650,950)
        ],
    )


# EXH is the exhibit in the *second* section P2 at physical 450 (label 350).
# ALT is the exhibit reached by the *same label 350* in the first section P1
# at physical 350.  Same label, different facility.
FAC_EXH = "EXH@P2"   # physical 450
FAC_ALT = "ALT@P1"   # physical 350


def assets_ab() -> list[Asset]:
    return [
        Asset(FAC_EXH, "L", Fraction(450)),
        Asset(FAC_ALT, "L", Fraction(350)),
    ]


def roster_book(reg: CatalogRegistry, assets: AssetsIndex) -> RosterBook:
    return RosterBook("BK1", "L", reg, assets)


# Both facilities are reachable: label 350 in P2 -> EXH, label 350 in P1 -> ALT.
SPANS = [
    {"section_id": "P2", "label_start": 340, "label_end": 360},  # [440,460) -> EXH
    {"section_id": "P1", "label_start": 340, "label_end": 360},  # [340,360) -> ALT
]


def shift(sid, fac, person, org, start, end):
    return {
        "shift_id": sid,
        "facility_id": fac,
        "person_id": person,
        "org_id": org,
        "start": start,
        "end": end,
    }


def three_sessions(facility: str):
    x = make_session("X", facility, 0, 8, "甲", "乙", 7)
    y = make_session("Y", facility, 0, 4, "甲", "乙", 4)
    z = make_session("Z", facility, 4, 8, "甲", "乙", 4)
    return x, y, z


def fmt_plan(plan) -> str:
    lines = [
        f"    total={plan.total_value}  selected={plan.selected_ids()}  "
        f"signable={list(plan.signable)}  unsigned={list(plan.unsigned)}"
    ]
    for p in plan.selected:
        s = p.session
        lines.append(
            f"      {s.session_id} [{s.window_start},{s.window_end}) "
            f"{s.org_a}+{s.org_b} value {s.value}"
        )
        for seg in p.segments:
            lines.append(
                f"        {seg.org_id} {seg.person_id}: [{seg.start},{seg.end}) "
                f"via shift {seg.shift_id} (roster {seg.roster_revision_id}, "
                f"catalog {seg.catalog_revision})"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


def main() -> int:
    reg = CatalogRegistry()
    reg.add(route_r1())
    assets = AssetsIndex(assets_ab())
    book = roster_book(reg, assets)
    planner = SettlementPlanner(book)
    x, y, z = three_sessions(FAC_EXH)
    ticket = make_ticket("T1", FAC_EXH, [x, y, z])

    print("=" * 78)
    print("Temporary-exhibition settlement demonstration")
    print("=" * 78)
    print(f"facility {FAC_EXH}: physical 450 (P2, label 350)")
    print(f"facility {FAC_ALT}: physical 350 (P1, label 350)  <- same label")

    # -- (0) baseline: one 甲, one 乙, both on [0,8) -------------------
    print("\n" + "-" * 78)
    print("(0) baseline: 小陈(甲) and 小周(乙) both on [0,8)")
    print("-" * 78)
    book.add_replace(
        "v1", "r1", SPANS,
        [
            shift("s_chen_1", FAC_EXH, "小陈", "甲", 0, 8),
            shift("s_zhou_1", FAC_EXH, "小周", "乙", 0, 8),
        ],
    )
    plan0 = planner.plan(ticket)
    print(fmt_plan(plan0))
    print("    -> X alone would pay 7, but Y+Z (adjacent, same two people)")
    print("       pays 8, so X is signable yet not selected.")

    contract0 = sign_contract("CT0", plan0)
    print(f"    signed contract {contract0.contract_id}: "
          f"total={contract0.total_value} sessions="
          f"{[p.session.session_id for p in contract0.sessions]}")
    for p in contract0.sessions:
        for seg in p.segments:
            print(f"      {p.session.session_id} {seg.org_id} {seg.person_id} "
                  f"[{seg.start},{seg.end})")

    # -- (1) reinforcement: two more 甲乙 witnesses on [0,8) ----------
    print("\n" + "-" * 78)
    print("(1) reinforcement: add 小李(甲) and 小赵(乙), also on [0,8)")
    print("-" * 78)
    book.add_replace(
        "v2", "r1", SPANS,
        [
            shift("s_chen_1", FAC_EXH, "小陈", "甲", 0, 8),
            shift("s_zhou_1", FAC_EXH, "小周", "乙", 0, 8),
            shift("s_li_1", FAC_EXH, "小李", "甲", 0, 8),
            shift("s_zhao_1", FAC_EXH, "小赵", "乙", 0, 8),
        ],
    )
    plan1 = planner.plan(ticket)
    print(fmt_plan(plan1))
    print("    -> different people witness X in [0,4) and Y in [0,4); the")
    print("       [0,8) X pair coexists with the Y/Z pairs -> X+Y+Z = 15.")

    # -- (2) relocation: move 小陈 [3,5) to the other same-label section
    print("\n" + "-" * 78)
    print("(2) relocation: back to one 甲/one 乙; 小陈's [3,5) moves to ALT@P1")
    print("-" * 78)
    book.add_replace(
        "v3", "r1", SPANS,
        [
            shift("s_chen_a", FAC_EXH, "小陈", "甲", 0, 3),
            shift("s_chen_b", FAC_ALT, "小陈", "甲", 3, 5),   # same label, other section
            shift("s_chen_c", FAC_EXH, "小陈", "甲", 5, 8),
            shift("s_zhou_1", FAC_EXH, "小周", "乙", 0, 8),
        ],
    )
    plan2 = planner.plan(ticket)
    print(fmt_plan(plan2))
    print("    -> during [3,5) nobody from 甲 is at EXH, so X, Y and Z all")
    print("       lose full-window witnessing. Nothing is selectable.")
    inv = invalid_witnesses(contract0, book.current())
    print(f"    old signed contract {contract0.contract_id} still names its people;")
    print("    re-checked against the current roster, these witnesses are invalid:")
    for w in inv:
        print(f"      {w.segment.session_id} {w.segment.org_id} "
              f"{w.segment.person_id} [{w.segment.start},{w.segment.end}) "
              f"-> {w.reason}")

    # -- on-site lookup with a cross-org dispute ----------------------
    print("\n" + "-" * 78)
    print("(3) on-site lookup: dispute, blanket exclusion, adjacent handover")
    print("-" * 78)
    book.add_replace(
        "v4", "r1", SPANS,
        [
            shift("d_chen_jia", FAC_EXH, "小陈", "甲", 0, 8),
            shift("d_chen_yi", FAC_EXH, "小陈", "乙", 2, 4),   # same person, both orgs
            shift("d_zhou", FAC_EXH, "小周", "乙", 0, 8),
        ],
    )
    look = book.lookup("v4", FAC_EXH)
    print("    timeline at EXH (v4):")
    for seg in look["segments"]:
        staff = ", ".join(f"{o}:{list(p)}" for o, p in seg["staff"].items()) or "(none)"
        disp = "; ".join(
            f"{d['person_id']} disputed by {d['orgs']} via {d['shift_ids']}"
            for d in seg["disputed"]
        )
        print(f"      [{seg['start']},{seg['end']})  usable {staff}"
              + (f"  | DISPUTE {disp}" if disp else ""))

    print("\n    adjacent handover (甲 hands over at time 4, no overlap):")
    book.add_replace(
        "v5", "r1", SPANS,
        [
            shift("h_chen", FAC_EXH, "小陈", "甲", 0, 4),
            shift("h_li", FAC_EXH, "小李", "甲", 4, 8),
            shift("h_zhou", FAC_EXH, "小周", "乙", 0, 8),
        ],
    )
    plan5 = planner.plan(
        make_ticket("T5", FAC_EXH, [make_session("FULL", FAC_EXH, 0, 8, "甲", "乙", 9)])
    )
    print(fmt_plan(plan5))

    # -- two staffing styles -----------------------------------------
    print("\n" + "-" * 78)
    print("(4) dense overlap vs. all-day staggered (same ticket, same value)")
    print("-" * 78)
    dense = [
        shift("o1", FAC_EXH, "a1", "甲", 0, 8),
        shift("o2", FAC_EXH, "a2", "甲", 0, 4),
        shift("o3", FAC_EXH, "a3", "甲", 4, 8),
        shift("o4", FAC_EXH, "b1", "乙", 0, 8),
        shift("o5", FAC_EXH, "b2", "乙", 2, 6),
    ]
    staggered = [
        shift("p1", FAC_EXH, "a1", "甲", 0, 4),
        shift("p2", FAC_EXH, "a2", "甲", 4, 8),
        shift("p3", FAC_EXH, "b1", "乙", 1, 5),
        shift("p4", FAC_EXH, "b2", "乙", 5, 8),
        shift("p5", FAC_EXH, "b3", "乙", 0, 1),
    ]
    for label, shifts in (("dense-overlap", dense), ("all-day-staggered", staggered)):
        bk = RosterBook("BK_" + label, "L", reg, assets)
        bk.add_replace("v1", "r1", SPANS, shifts)
        p = SettlementPlanner(bk).plan(
            make_ticket("T", FAC_EXH,
                        [make_session("W", FAC_EXH, 0, 8, "甲", "乙", 11)])
        )
        print(f"    {label}: {fmt_plan(p).strip()}")

    print("\n" + "=" * 78)
    print("Done. All numbers are exact Fractions; nothing is rounded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
