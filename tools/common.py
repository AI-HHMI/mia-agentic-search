"""Shared helpers: paths, YAML loading, schema validator, identity keys."""
import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema" / "dataset.schema.json"
DATASETS_DIR = ROOT / "datasets"
STATE_DIR = ROOT / "state"
RUNS_DIR = STATE_DIR / "runs"
REJECTED_PATH = STATE_DIR / "rejected.yaml"
# Automatic rejections live on an unprotected branch, since workflows cannot push to protected main.
REJECTIONS_REF = "origin/rejections"
FRONTIER_DIR = STATE_DIR / "frontier"  # one file per routine avoids cross-PR conflicts


class _NoDatesLoader(yaml.SafeLoader):
    """SafeLoader that keeps dates/timestamps as strings so they match the JSON Schema."""


_NoDatesLoader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_yaml(path):
    with open(path) as f:
        return yaml.load(f, Loader=_NoDatesLoader)


def load_rejected():
    """Rejected keys: state/rejected.yaml in the working tree (manual) + the rejections branch (automatic)."""
    keys = []
    if REJECTED_PATH.exists():
        keys += (load_yaml(REJECTED_PATH) or {}).get("rejected") or []
    out = subprocess.run(["git", "show", f"{REJECTIONS_REF}:state/rejected.yaml"], cwd=ROOT,
                         capture_output=True, text=True, check=False)
    if out.returncode == 0:
        keys += (yaml.load(out.stdout, Loader=_NoDatesLoader) or {}).get("rejected") or []
    return list(dict.fromkeys(keys))


def dump_yaml(data, path):
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=100)


def load_schema():
    return json.loads(SCHEMA_PATH.read_text())


_checker = FormatChecker(formats=())


@_checker.checks("uri", raises=ValueError)
def _is_uri(value):
    if not isinstance(value, str):
        return True
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https", "ftp", "s3") or not parts.netloc:
        raise ValueError(f"not an absolute http(s)/ftp/s3 URL: {value!r}")
    return True


@_checker.checks("date", raises=ValueError)
def _is_date(value):
    if isinstance(value, str):
        dt.date.fromisoformat(value)
    return True


@_checker.checks("date-time", raises=ValueError)
def _is_datetime(value):
    if isinstance(value, str):
        if "T" not in value:
            raise ValueError("date-time must contain 'T'")
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("date-time must include a timezone (e.g. Z)")
    return True


def validator():
    return Draft202012Validator(load_schema(), format_checker=_checker)


def iter_record_paths(paths=None):
    targets = [Path(p).resolve() for p in paths] if paths else [DATASETS_DIR]
    for t in targets:
        if t.is_dir():
            yield from sorted(t.rglob("*.yaml"))
        elif t.suffix == ".yaml":
            yield t


def rel(path):
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def normalize_url(url):
    if not url:
        return None
    p = urlsplit(url.strip().lower())
    host = p.netloc.removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def normalize_title(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def identity_keys(record):
    """Stable keys used for dedup, strongest first."""
    keys = []
    if record.get("doi"):
        keys.append("doi:" + record["doi"].lower())
    if record.get("accession"):
        keys.append(f"acc:{record.get('repository', '').lower()}:{record['accession'].lower()}")
    if record.get("landing_url"):
        keys.append("url:" + normalize_url(record["landing_url"]))
    return keys


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
