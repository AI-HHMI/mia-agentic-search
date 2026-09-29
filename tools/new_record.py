"""Create a record skeleton that the agent then fills in.

    python tools/new_record.py --id empiar-10311-hela-fib-sem --repository EMPIAR --by harvest-repositories

Every field is present. Unknown optional values are null; fields the agent MUST
fill are set to the string "TODO", which fails validation until replaced.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import DATASETS_DIR, ROOT, dump_yaml, load_schema, utcnow  # noqa: E402

TODO = "TODO"


def skeleton(rec_id, repository, discovered_by):
    now = utcnow()
    return {
        "schema_version": "1.1",
        "id": rec_id,
        "title": TODO,
        "short_name": TODO,
        "description": TODO,
        "landing_url": TODO,
        "doi": None,
        "repository": repository,
        "accession": None,
        "authors": [],
        "publications": [],
        "date_published": None,
        "imaging": {"modality": [TODO], "dimensionality": TODO, "voxel_size_nm": None,
                    "channels": None, "organism": [], "sample": None, "labels_stains": []},
        "data": {"formats": [TODO], "size_bytes": None, "n_items": None, "access": TODO,
                 "download_url": None, "download_method": TODO},
        "annotations": {"present": TODO, "types": [], "coverage": "unknown", "format": None,
                        "source": "unknown"},
        "ml": {"tasks": [TODO], "splits_provided": False, "benchmark": None, "baseline_code_url": None},
        "license": {"spdx": "unknown", "url": None},
        "provenance": {"discovered_by": discovered_by, "discovered_at": now,
                       "evidence_urls": [TODO], "confidence": TODO},
        "verification": {"url_ok": False, "license_found": False, "annotations_verified": False,
                         "last_checked": now},
        "notes": None,
    }


def main():
    schema = load_schema()
    repos = schema["properties"]["repository"]["enum"]
    routines = schema["properties"]["provenance"]["properties"]["discovered_by"]["enum"]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True, help="lowercase slug, e.g. empiar-10311-hela-fib-sem")
    ap.add_argument("--repository", required=True, choices=repos)
    ap.add_argument("--by", required=True, choices=routines, help="discovering routine")
    a = ap.parse_args()

    path = DATASETS_DIR / a.repository.lower() / f"{a.id}.yaml"
    if path.exists():
        sys.exit(f"exists: {path.relative_to(ROOT)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml(skeleton(a.id, a.repository, a.by), path)
    print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
