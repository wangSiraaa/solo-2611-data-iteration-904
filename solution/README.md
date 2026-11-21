# Route chainage catalog + inspection coverage facts

Python 3.11 / SQLite. Synthetic data only — no real railway standard is
involved. The route is a physical axis `[0, L)` tiled by positive-length
half-open segments; labels within a segment are `label = physical -
physical_start + label_start` (slope fixed at 1). Labels are **not** a
global coordinate: they may repeat and may have gaps.

## Why labels cannot be a continuous coordinate

The materials' r1 route:

| section | physical `[start, end)` | label `[start, end)` |
|---------|-------------------------|----------------------|
| P1      | `[0, 400)`              | `[0, 400)`           |
| P2      | `[400, 700)`            | `[300, 600)`         |
| P3      | `[700, 1000)`           | `[650, 950)`         |

* label `350` has **two** physical candidates: P1@350 and P2@450. Treating
  `350` as one coordinate would silently swap the inspected object.
* label `620` is a **gap** (P2 ends at 600, P3 starts at 650): unresolved.
* `620` resolves only after the r2 base correction (P2 label base 300 → 330
  widens P2's labels to `[330, 630)`).

The point reverse lookup therefore returns **all** candidates and never
picks a nearest value:

| candidates | status       |
|------------|--------------|
| 0          | `unresolved` |
| 1          | `resolved`   |
| >1         | `ambiguous`  |

A caller may pass an explicit `section_id` to disambiguate; an unknown
section is reported `unresolved`, never guessed.

## Half-open semantics (the subtle part)

Point membership and interval endpoints are treated differently, on purpose:

* A physical **point** exactly on a segment's right end does **not** belong
  to that segment (`start <= p < end`).
* An interval's **upper bound** may equal a segment's exclusive end label.
  Converting a span never re-tests point membership for its upper bound, so
  a legal end-of-section span is accepted. The resulting physical interval
  is still half-open, which correctly excludes a facility sitting exactly
  at the boundary.

The `merge_intervals` union merges overlapping **and touching** intervals,
so `sum(length)` is the true covered measure — repeated or overlapping
spans are never double counted.

## Facilities

Each facility has a stable `asset_id` and a fixed **physical** position that
does not move when a label base changes. A facility counts as covered only
when its physical point lies in the merged physical coverage
(`start <= p < end`). Facility hits are **never** derived from a label
point lookup, so an ambiguous or unresolved label can never manufacture a
hit.

## Independent catalog vs. inspection facts

`chainage_catalog.py` knows nothing about jobs or reports: it can resolve
points and convert intervals on its own. `inspection_library.py` depends on
it, not the reverse. Storage is two separate schemas:

* **catalog**: `route_revision`, `route_section` (exact rationals stored as
  `n/d` text plus a sortable key for indexed range prefiltering).
* **inspection facts**: `asset`, `report`, `report_span`,
  `report_interval`, `report_facility`, `applied_op`.

Reports record the original label request, the selected section, the actual
physical coverage, the covered facility identities, and the catalog
revision. `report_id` is content-addressed: identical content is idempotent,
differing content under the same id is rejected.

## Revision behaviour

`revise_chainage` changes **only** the named section's `label_start`
(honouring the material's explicit base correction). Physical extents,
section order, and every other section are copied verbatim — this is not a
route editor, and no other edits are inferred.

After `r2` (P2 base 300 → 330), the *same* request `P2 [350,390)`:

* r1: physical `[450, 490)` → covers **B**@470 (not C@440).
* r2: physical `[420, 460)` → covers **C**@440 (not B@470).

The facility identities and physical positions are unchanged; only the
label→physical mapping moved. Old report R1 keeps explaining the original
labels and positions (`stale=true` once r2 is current).

---

# Temporary-exhibition settlement (临展结算)

Two new modules build on the same catalogue and physical-facility identities:

* `exhibition_roster.py` — roster books (班簿): versioned, replace/revoke,
  cross-org attribution, per-segment on-site lookup.
* `exhibition_settlement.py` — sessions and tickets: exact optimal selection,
  auditable per-session/per-segment rendering, frozen contracts, and
  post-hoc invalidation of an old contract's witnesses.

Nothing here changes the catalogue or inspection modules; the settlement
layer only *consumes* their label→physical conversion and facility identity.

## Rosters (班簿) and attribution

A roster revision is a **full snapshot** resolved against a named catalogue
revision.  It names a `section_id` and half-open label spans, so its union
identifies exactly which facilities it covers.  **Two sections may carry the
same label**; because facilities have fixed physical identities, moving a
presence to "another section with the same label" moves it to a *different
facility*.  Every shift records `person, org, facility` and a half-open
presence `[start, end)`.

A later revision either **replaces** the snapshot wholesale or **revokes** it.
Every revision remains queryable as its own on-site snapshot (截面).

**Dispute rule.** If the *same person* is recorded by two different
organisations at the *same facility* with overlapping presence, then over the
overlap neither organisation may use that person.  Everyone else, and every
other interval, stays usable.  Adjacent presence may hand over at a boundary
(`end == next.start`) with no gap.

`RosterBook.lookup(revision, facility)` returns elementary segments — every
shift boundary is a breakpoint — with usable staff per org and the raw shifts
that caused each dispute:

```
[0, 2)  乙: 小周, 甲: 小陈
[2, 4)  乙: 小周
  DISPUTE 小陈 recorded by ['乙', '甲'] (shifts ['d_chen_jia', 'd_chen_yi'])
          -> excluded for both
[4, 8)  乙: 小周, 甲: 小陈
```

## Tickets, sessions and exact selection

One venue (one facility) issues **one ticket for one day**, holding at most
20 **sessions**.  Each session fixes a complete, immovable half-open viewing
window, **two distinct contracted orgs**, and a whole-session payment.  A
session pays only in full and only if, for its **whole** window, each org can
field one qualified person.  A witness may hand over inside a session, but one
person witnesses only one session at an instant.

Take usable-segment endpoints plus session bounds; they cut the day into
cells where the candidate pool and the active sessions are constant.  Because
capacity resets at every instant, feasibility decomposes per cell: for each
`(cell, org)` the number of selected sessions needing it must not exceed the
usable head-count.  The optimum is a maximum-weight subset of sessions under
these per-cell capacities, solved **exactly** by branch and bound (n ≤ 20)
with a greedy incumbent.

Example (the task's scenario), one 甲 + one 乙 over `[0,8)`, sessions
`X=[0,8)/7`, `Y=[0,4)/4`, `Z=[4,8)/4`:

| staff                            | optimal | value |
|----------------------------------|---------|-------|
| 小陈(甲), 小周(乙), both `[0,8)`  | `Y+Z`   | 8     |
| + 小李(甲), 小赵(乙) `[0,8)`      | `X+Y+Z` | 15    |
| 小陈 `[3,5)` moved to same-label P1 | none | 0     |

All three are individually signable in the first case — `X` alone pays 7, but
`Y+Z` pays 8, so `X` is signable yet not selected.  Ties yield any one
optimal witness (deterministic: higher value first, then session id).

## Contracts: freeze, re-plan, invalidate

`sign_contract` freezes the **catalogue revision, roster revision and
allocation** together with a content fingerprint.  After a later roster
revision or revocation, `SettlementPlanner.plan` re-plans from the new
snapshot, while `invalid_witnesses(contract, current)` re-checks the *frozen*
witnesses and reports exactly which no longer hold, with a reason:

| reason              | meaning                                            |
|---------------------|----------------------------------------------------|
| `roster_revoked`    | the whole roster was withdrawn                     |
| `person_not_present`| the person has no presence overlapping the segment |
| `presence_gap`      | present for only part of the segment               |
| `cross_org_dispute` | present under two orgs at some instant             |
| `not_usable_for_org`| present throughout, but only under the wrong org   |

The contract object itself is immutable and keeps naming the people it was
signed with; only the re-check marks which of them are now unsupported.

## Running the exhibition layer

```bash
python3 solution/exhibition_demo.py                 # the three changes + cases
python3 -m unittest discover -s solution/tests -v   # all tests
python3 solution/exhibition_cli.py books
python3 solution/exhibition_cli.py lookup --revision v4 --facility EXH@P2
python3 solution/exhibition_cli.py plan --ticket T1 --revision v1 --sign CT0 --db exh.db
python3 solution/exhibition_cli.py invalidate --contract CT0 --db exh.db
```

The standalone CLI (`exhibition_cli.py`) needs only `materials/exhibition/
scenario.json` and is the on-site entry point: it prints per-segment staffing,
the optimal auditable selection, and which frozen witnesses a later revision
invalidated.  The demo also exercises a cross-org dispute, an adjacent
handover, and dense-overlap vs. all-day-staggered staffing reaching the same
value by different people.

## Cost

Selection is exact; worst case is the combinatorial limit (20 fully
overlapping sessions, capacity 10) at ~0.5 s; typical and disjoint tickets are
sub-millisecond.  Lookup is O(#shifts) per facility; contracts are persisted
in the separate `contract*` schema and reload-ready.

---

## Running (catalogue + inspection)

```bash
python3 solution/run_demo.py                    # in-memory
python3 solution/run_demo.py --db route.db      # durable (resumable)
python3 -m unittest discover -s solution/tests -v
python3 solution/bench_scale.py                 # 20 sections / 500 assets / 200 jobs
```

`run_demo.py` reads `materials/730` operation by operation, applies the P2
base correction, and prints the real facility change (B in r1 → C in r2).
A file-backed DB records an op-log so re-running resumes (mutating ops are
skipped, read-only ops still render).

## Scale / cost (20 sections, 500 facilities, 200 jobs)

Measured by `bench_scale.py`:

* **Point reverse lookup** — per-section scan, ~39 µs/query at 20 sections
  (~11 % ambiguous, ~88 % unresolvable in the stress route). A per-revision
  label-range index would make this `O(log n + k)` for the k duplicate
  candidates. Duplicate labels mean the result is a *set*, so no index can
  legitimately collapse it to one value.
* **Batch conversion** — ~0.18 ms/job: convert spans `O(#spans)`, merge
  `O(spans log spans)`, facility scan `O(#assets)`. 500 assets × 200 jobs =
  100k point checks; a physical-position index would reduce the facility
  side to an interval query per job.
* **Revision invalidation** — building the new revision is ~0.05 ms (copy
  the section rows). Marking old reports stale is metadata-only. The real
  work is re-resolving jobs that must be republished, `O(jobs × (spans +
  assets))`; in the stress route moving one section's base pushed 14/200
  spans outside the moved section's label range, and those jobs are
  correctly rejected rather than remapped.

## Explicit non-goals

No path optimisation, no measurement adjustment/least-squares, no inference
of missing positions. Nothing beyond the one explicit base correction is
changed.
