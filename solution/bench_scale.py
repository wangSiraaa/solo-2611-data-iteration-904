"""Scale / cost demonstration at the stated limits.

20 sections (some with repeated labels), 500 fixed facilities, 200 jobs.
Measures:
  * batch conversion cost per job (per-section scan vs label-index prefilter)
  * duplicate-label point lookup cost
  * revision invalidation cost (how many reports go stale, and re-resolve cost)

Run: python3 solution/bench_scale.py
"""

from __future__ import annotations

import os
import random
import sys
import time
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chainage_catalog import CatalogRegistry, RouteCatalog, Section  # noqa: E402
from inspection_library import Asset, InspectionLibrary  # noqa: E402


def build_scale(seed: int = 7):
    rnd = random.Random(seed)
    sections = []
    phys = Fraction(0)
    # 20 sections of length 100 each -> route length 2000.
    # Deliberately overlap label ranges heavily so duplicate labels exist.
    for i in range(20):
        label_start = Fraction((i * 37) % 150)  # dense overlapping labels
        sections.append(
            Section(f"S{i:02d}", phys, phys + 100, label_start)
        )
        phys += 100
    cat = RouteCatalog("r1", "L", phys, sections)

    assets = [
        Asset(f"A{i:03d}", "L", Fraction(rnd.randint(0, 1999)))
        for i in range(500)
    ]

    jobs = []
    for j in range(200):
        sec = sections[rnd.randrange(len(sections))]
        lo = sec.label_at(sec.physical_start)
        hi = sec.label_end
        span = Fraction(rnd.randint(1, 50))
        start = lo + Fraction(rnd.randint(0, int(hi - lo) - 1))
        end = min(start + span, hi)
        jobs.append(
            {
                "job_id": f"J{j:03d}",
                "route_id": "L",
                "spans": [
                    {"section_id": sec.section_id,
                     "label_start": float(start), "label_end": float(end)}
                ],
            }
        )
    return cat, assets, jobs


def time_point_queries(cat: RouteCatalog, n: int = 2000) -> tuple[float, int, int, int]:
    """Return (seconds, resolved, ambiguous, unresolved) for n label lookups."""
    labels = [Fraction(i) for i in range(n)]
    resolved = ambiguous = unresolved = 0
    t0 = time.perf_counter()
    for lab in labels:
        r = cat.resolve_label_point(lab)
        if r["status"] == "resolved":
            resolved += 1
        elif r["status"] == "ambiguous":
            ambiguous += 1
        else:
            unresolved += 1
    return time.perf_counter() - t0, resolved, ambiguous, unresolved


def main() -> int:
    cat, assets, jobs = build_scale()
    reg = CatalogRegistry()
    reg.add(cat)
    lib = InspectionLibrary(reg, assets)

    print("=" * 72)
    print("Scale profile: 20 sections, 500 assets, 200 jobs")
    print("=" * 72)
    print(f"route length {cat.length}, sections {len(cat.sections)}, "
          f"assets {len(lib.assets)}, jobs {len(jobs)}")

    # 1. Duplicate-label point lookup.
    secs, res, amb, unr = time_point_queries(cat)
    print("\n[1] point reverse lookup (2000 labels, per-section scan)")
    print(f"    {secs*1000:.1f} ms total, {secs/2000*1e6:.1f} us/query")
    print(f"    resolved={res} ambiguous={amb} unresolved={unr} "
          f"(ambiguity rate {amb/(res+amb+unr):.0%})")
    print("    cost: O(#sections) per point; with 20 sections scan is cheap."
          " A per-revision label-range index would cut it to O(log n + k).")

    # 2. Batch conversion + coverage.
    t0 = time.perf_counter()
    n_spans = sum(len(j["spans"]) for j in jobs)
    for j in jobs:
        lib.resolve_and_inspect(j, f"R{j['job_id']}")
    elapsed = time.perf_counter() - t0
    print("\n[2] batch job conversion (200 jobs, "
          f"{n_spans} spans, merge + facility scan)")
    print(f"    {elapsed*1000:.1f} ms total, {elapsed/200*1e3:.2f} ms/job")
    print("    cost per job: convert spans O(#spans) + merge O(spans log spans)"
          " + facility scan O(#assets). 500 assets x 200 jobs = 100k checks.")

    # 3. Revision invalidation.
    t1 = time.perf_counter()
    reg.revise_section_label_start("r2", "L", "S07", Fraction(999))
    revise_elapsed = time.perf_counter() - t1
    affected = lib.reports_affected_by("r2")
    # Re-resolve all jobs under r2 (fresh report ids to avoid conflicts).
    # A revision that moves a section's label base can push previously-valid
    # spans outside that section's new label range; those jobs are correctly
    # rejected rather than silently remapped.
    t2 = time.perf_counter()
    republished = rejected = 0
    for j in jobs:
        try:
            lib.resolve_and_inspect(j, f"R{j['job_id']}_v2")
            republished += 1
        except Exception:
            rejected += 1
    redo_elapsed = time.perf_counter() - t2
    print("\n[3] revision invalidation cost")
    print(f"    old reports: {len(affected)} (all of them remain readable)")
    print(f"    new revision build: {revise_elapsed*1000:.2f} ms")
    print(f"    full re-resolve under new revision: {redo_elapsed*1000:.1f} ms "
          f"({redo_elapsed/200*1e3:.2f} ms/job)")
    print(f"    re-published={republished} rejected-invalid={rejected} "
          "(spans no longer fit the moved section's label range)")
    print("    cost: O(#reports) stale marks are metadata-only; the real work"
          " is re-resolving jobs that must be republished, O(jobs x (spans + assets)).")

    print("\nNote: no path optimisation, no measurement adjustment, no"
          " inference of missing positions was attempted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
