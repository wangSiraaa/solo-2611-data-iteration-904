"""Standalone on-site entry points: roster lookup and ticket settlement.

This is the command an on-site clerk can run by itself, against a scenario
file, without starting the rest of the system:

    # per-segment staffing of a facility under one roster revision (截面)
    python3 solution/exhibition_cli.py lookup --revision v4 --facility EXH@P2

    # which revision of a book is current, and all their spans
    python3 solution/exhibition_cli.py books

    # select the highest-paying signable combination and print the audit
    python3 solution/exhibition_cli.py plan --ticket T1 --revision v1 --sign CT0

    # after a newer revision, re-check what the old contract lost
    python3 solution/exhibition_cli.py invalidate --contract CT0

The scenario file is ``materials/exhibition/scenario.json`` by default and can
be overridden with ``--scenario``.  Numbers stay exact (Fractions); intervals
are half-open ``[start, end)`` throughout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chainage_catalog import (  # noqa: E402
    CatalogRegistry,
    RouteCatalog,
    as_fraction,
    fraction_to_db,
)
from inspection_library import Asset, AssetsIndex  # noqa: E402

from exhibition_roster import RosterBook, RosterQueryError, make_shift  # noqa: E402
from exhibition_settlement import (  # noqa: E402
    SettlementError,
    SettlementPlanner,
    invalid_witnesses,
    load_contract,
    make_session,
    make_ticket,
    persist_contract,
    sign_contract,
)


def _frac(db_text: str) -> str:
    v = Fraction(db_text)
    return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"


def load_scenario(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    reg = CatalogRegistry()
    reg.add(RouteCatalog.from_dict(data["route"]))
    assets = AssetsIndex([Asset.from_dict(a) for a in data["assets"]])

    book_data = data["roster_book"]
    book = RosterBook(book_data["book_id"], book_data["route_id"], reg, assets)
    for rev in book_data["revisions"]:
        if rev["kind"] == "revoke":
            book.add_revoke(rev["revision_id"], rev.get("catalog_revision"))
        else:
            book.add_replace(
                rev["revision_id"],
                rev["catalog_revision"],
                rev["spans"],
                [make_shift(**s) for s in rev["shifts"]],
            )

    tickets = {}
    for t in data.get("tickets", []):
        sessions = [
            make_session(
                s["session_id"], t["facility_id"], s["window_start"],
                s["window_end"], s["org_a"], s["org_b"], s["value"],
            )
            for s in t["sessions"]
        ]
        tickets[t["ticket_id"]] = make_ticket(t["ticket_id"], t["facility_id"], sessions)
    return reg, assets, book, tickets


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_books(book: RosterBook) -> None:
    print(f"book {book.book_id} (route {book.route_id})")
    for rev in book.all_revisions():
        cur = "  <- current" if rev is book.current() else ""
        print(f"  {rev.revision_id} [{rev.kind}] catalog {rev.catalog_revision}{cur}")
        if rev.revoked:
            print("    (revoked)")
            continue
        for sp in rev.spans:
            print(
                f"    {sp.section_id} label [{_frac(fraction_to_db(sp.label_start))}, "
                f"{_frac(fraction_to_db(sp.label_end))}) -> physical "
                f"[{_frac(fraction_to_db(sp.physical_start))}, "
                f"{_frac(fraction_to_db(sp.physical_end))})"
            )
        print(f"    covered facilities: {list(rev.covered_facilities)}")


def cmd_lookup(book: RosterBook, revision_id: str, facility: str) -> None:
    view = book.lookup(revision_id, facility)
    tag = " (revoked)" if view["revoked"] else ""
    print(
        f"book {view['book_id']} revision {view['revision_id']} "
        f"catalog {view['catalog_revision']}{tag}"
    )
    print(f"facility {facility} covered={view['covered']}")
    if not view["segments"]:
        print("  (no presence recorded at this facility)")
        return
    for seg in view["segments"]:
        staff = ", ".join(
            f"{org}: {', '.join(people)}" for org, people in seg["staff"].items()
        ) or "(nobody usable)"
        print(f"  [{_frac(seg['start'])}, {_frac(seg['end'])})  {staff}")
        for d in seg["disputed"]:
            print(
                f"    DISPUTE {d['person_id']} recorded by {d['orgs']} "
                f"(shifts {d['shift_ids']}) -> excluded for both"
            )


def cmd_plan(book: RosterBook, ticket, revision_id: str, sign_id, db_path=None) -> None:
    planner = SettlementPlanner(book)
    plan = planner.plan(ticket, revision_id)
    print(
        f"ticket {ticket.ticket_id} facility {ticket.facility_id} "
        f"revision {plan.roster_revision_id} catalog {plan.catalog_revision}"
    )
    print(
        f"total={_frac(fraction_to_db(plan.total_value))}  "
        f"selected={plan.selected_ids()}  signable={list(plan.signable)}  "
        f"unsigned={list(plan.unsigned)}"
    )
    for p in plan.selected:
        s = p.session
        print(
            f"  {s.session_id} [{_frac(fraction_to_db(s.window_start))},"
            f"{_frac(fraction_to_db(s.window_end))}) {s.org_a}+{s.org_b} "
            f"value {_frac(fraction_to_db(s.value))}"
        )
        for seg in p.segments:
            print(
                f"    {seg.org_id} {seg.person_id}: "
                f"[{_frac(fraction_to_db(seg.start))},"
                f"{_frac(fraction_to_db(seg.end))}) "
                f"via shift {seg.shift_id}"
            )
    if sign_id:
        contract = sign_contract(sign_id, plan)
        print(
            f"  signed contract {contract.contract_id}: "
            f"total={_frac(fraction_to_db(contract.total_value))} "
            f"sessions={[p.session.session_id for p in contract.sessions]}"
        )
        if db_path:
            import sqlite3

            from exhibition_settlement import ensure_settlement_schema, persist_contract

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            ensure_settlement_schema(conn)
            try:
                persist_contract(conn, contract)
            finally:
                conn.close()
            print(f"  persisted to {db_path}")


def cmd_invalidate(book: RosterBook, contract, against_id=None) -> None:
    current = book.get(against_id) if against_id else book.current()
    bad = invalid_witnesses(contract, current)
    print(
        f"contract {contract.contract_id} frozen at roster "
        f"{contract.roster_revision_id}; checked against {current.revision_id}"
    )
    if not bad:
        print("  all witnesses still valid")
        return
    print(f"  {len(bad)} witness segment(s) no longer justifiable:")
    for w in bad:
        print(
            f"    {w.segment.session_id} {w.segment.org_id} "
            f"{w.segment.person_id} "
            f"[{_frac(fraction_to_db(w.segment.start))},"
            f"{_frac(fraction_to_db(w.segment.end))}) -> {w.reason}"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    default = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "materials", "exhibition", "scenario.json",
    )
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default=default)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("books", help="list roster revisions")

    p_lookup = sub.add_parser("lookup", help="per-segment staffing at a facility")
    p_lookup.add_argument("--revision", required=True)
    p_lookup.add_argument("--facility", required=True)

    p_plan = sub.add_parser("plan", help="optimal signable selection")
    p_plan.add_argument("--ticket", required=True)
    p_plan.add_argument("--revision", default=None)
    p_plan.add_argument("--sign", default=None, metavar="CONTRACT_ID")
    p_plan.add_argument("--db", default=None, help="persist a signed contract here")

    p_inv = sub.add_parser("invalidate", help="re-check a frozen contract")
    p_inv.add_argument("--contract", required=True)
    p_inv.add_argument("--against", default=None, metavar="REVISION_ID")
    p_inv.add_argument("--db", default=None)

    args = parser.parse_args(argv)
    reg, assets, book, tickets = load_scenario(args.scenario)

    if args.cmd == "books":
        cmd_books(book)
    elif args.cmd == "lookup":
        cmd_lookup(book, args.revision, args.facility)
    elif args.cmd == "plan":
        if args.ticket not in tickets:
            parser.error(f"unknown ticket {args.ticket!r}")
        cmd_plan(book, tickets[args.ticket], args.revision, args.sign, args.db)
    elif args.cmd == "invalidate":
        # Contracts are read from the durable DB when one is supplied.
        if not args.db:
            parser.error("invalidate needs --db holding the signed contract")
        import sqlite3

        from exhibition_settlement import ensure_settlement_schema

        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        ensure_settlement_schema(conn)
        try:
            contract = load_contract(conn, args.contract)
        finally:
            conn.close()
        cmd_invalidate(book, contract, args.against)
    return 0


if __name__ == "__main__":
    sys.exit(main())
