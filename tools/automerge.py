"""Auto-merge policy for dataset PRs; `tools/labels.py sync` applies it with GitHub's native auto-merge.

A PR qualifies when the record on its head gets these labels (tools/labels.py:labels_for):
  - enriched                                 the enricher has inspected the files
  - dim:2D, dim:2D+t, dim:3D or dim:3D+t     any dimensionality, as long as it is set
  - license:<spdx> other than license:unknown (any license, incl. custom, for now)
  - at least one fmt:… and no fmt:other      every data format is known
and the PR is not a draft, has no HOLD_LABELS, and isn't a possible duplicate (tools/common.py:similarity_reasons)
of a record on main or another open PR, unless a human labelled it NOT_DUPLICATE. The `validate` check is left
to GitHub: the ruleset on main requires it, so `gh pr merge --auto` waits for it.

Edit the constants below to change the policy.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import similarity_reasons  # noqa: E402

REQUIRED = ["enriched"]
ALLOWED_DIMS = ["dim:2D", "dim:2D+t", "dim:3D", "dim:3D+t"]  # all four; must just be set
# The license only has to be known for now (not `unknown`); which licenses are acceptable is decided later.
# Set this to a list of SPDX ids to allow only those again (e.g. ["CC0-1.0", "CC-BY-4.0", "BSD-3-Clause"]).
ALLOWED_LICENSES = None
HOLD_LABELS = ["hold", "do-not-merge"]
NOT_DUPLICATE = "not-duplicate"  # human-only: a reviewer checked the possible duplicates


def criteria_failures(labels):
    """Reasons a record with these labels doesn't qualify (empty list = qualifies)."""
    fails = [f"missing `{lab}`" for lab in REQUIRED if lab not in labels]
    if not set(labels) & set(ALLOWED_DIMS):
        dims = [lab for lab in labels if lab.startswith("dim:")]
        fails.append(f"dimensionality not allowed ({', '.join(dims) or 'no dim label'})")
    lic = [lab.removeprefix("license:") for lab in labels if lab.startswith("license:")]
    if not lic or "unknown" in lic:
        fails.append("license unknown")
    elif ALLOWED_LICENSES is not None and not set(lic) & set(ALLOWED_LICENSES):
        fails.append(f"license not on the allow-list ({', '.join(lic)})")
    fmts = [lab for lab in labels if lab.startswith("fmt:")]
    if not fmts:
        fails.append("no data format")
    elif "fmt:other" in fmts:
        fails.append("a data format is `other` (unknown)")
    return fails


def duplicate_reasons(rec, others):
    """'possible duplicate of <id> (...)' for each record in `others` that looks like the same dataset."""
    return [f"possible duplicate of {o.get('id')} ({', '.join(why)})"
            for o in others if o is not rec and o.get("id") != rec.get("id") and (why := similarity_reasons(rec, o))]


def failures(labels, pr, rec, others):
    """Why PR `pr` (gh JSON with labels, isDraft) carrying `rec`, whose labels are `labels`, isn't merged."""
    have = {lab["name"] for lab in pr.get("labels") or []}
    fails = criteria_failures(labels)
    if pr.get("isDraft"):
        fails.append("draft")
    if held := sorted(have & set(HOLD_LABELS)):
        fails.append(f"on hold ({', '.join(held)})")
    if NOT_DUPLICATE not in have:
        fails += duplicate_reasons(rec, others)
    return fails
