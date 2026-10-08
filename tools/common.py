"""Shared helpers: paths, YAML loading, schema validator, identity keys."""
import datetime as dt
import functools
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
DRAFTS_DIR = ROOT / "drafts"  # harvesters draft records here (git-ignored); publish.py files them in datasets/
STATE_DIR = ROOT / "state"
RUNS_DIR = STATE_DIR / "runs"
REJECTED_PATH = STATE_DIR / "rejected.yaml"
# Automatic rejections live on an unprotected branch, since workflows cannot push to protected main.
REJECTIONS_REF = "origin/rejections"
FRONTIER_DIR = STATE_DIR / "frontier"  # one file per routine avoids cross-PR conflicts
RUN_BUDGET_MIN = 120      # hard upper runtime per agent run
HARVEST_TARGET = 10       # a harvest run stops after publishing this many new datasets (one PR each)
SEARCH_CUTOFF_MIN = 110   # stop searching here, leaving time to publish PRs and save state
DATASET_BRANCH_PREFIX = "claude/dataset/"  # one branch + PR per proposed dataset
STATE_BRANCH_PREFIX = "claude/state/"      # per-routine frontier + run logs, never reviewed

# Enricher: technical inspection of proposed datasets (see .claude/skills/enrich-prs).
WHOLE_DOWNLOAD_MAX_BYTES = 50 * 10**9      # download queue: whole dataset only below this, and only for a confirmed size
SAMPLE_MAX_BYTES = 500 * 10**6             # per dataset and run, summed over all tools/sample.py downloads
CONFIRMED_SIZE_SOURCES = ("file-listing",)  # technical.size_source values that count as a confirmed size
# Version-control / OS files inside archives and folders: not part of the dataset.
JUNK_PATH = re.compile(r"(^|/)(\.svn|\.git|__MACOSX)/|(^|/)(\.DS_Store|Thumbs\.db|\._[^/]*)$")
USER_AGENT = "mia-agentic-search/1.0 (metadata inspection; https://github.com/AI-HHMI/mia-agentic-search)"

# Caps tie provenance.confidence (internal only, not shown in PRs) to what was actually verified.
CONFIDENCE_CAPS = [("url_ok", 0.5, "landing page not reachable"),
                   ("license_found", 0.7, "no license found"),
                   ("annotations_verified", 0.85, "annotation files not seen in a file listing")]

# Hosts that count as "the paper was read" (full text or article page).
PAPER_HOSTS = ("doi.org", "europepmc.org", "ebi.ac.uk/europepmc", "ncbi.nlm.nih.gov", "biorxiv.org",
               "api.biorxiv.org", "medrxiv.org", "arxiv.org", "nature.com", "cell.com", "sciencedirect.com",
               "springer.com", "wiley.com", "elifesciences.org", "rupress.org", "plos.org", "frontiersin.org",
               "biomedcentral.com", "oup.com", "science.org", "pnas.org", "openreview.net", "thecvf.com",
               "mlr.press", "neurips.cc", "ieee.org", "acm.org", "embopress.org", "journals.biologists.com")


def git(*args, check=False):
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)
    return out.stdout if out.returncode == 0 else ""


def git_objects(specs):
    """Read many `<rev>:<path>` objects with one `git cat-file --batch`: {spec: (type, bytes)}, missing ones left out.

    One process instead of one per branch matters on slow filesystems (NFS), where each git call costs ~0.1-1 s.
    """
    specs = list(dict.fromkeys(specs))
    if not specs:
        return {}
    out = subprocess.run(["git", "cat-file", "--batch"], cwd=ROOT, input="\n".join(specs).encode() + b"\n",
                         capture_output=True, check=False).stdout
    found, pos = {}, 0
    for spec in specs:
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        pos = end + 1
        if len(header) != 3:  # "<spec> missing" / "ambiguous"
            continue
        size = int(header[2])
        found[spec] = (header[1].decode(), out[pos:pos + size])
        pos += size + 1
    return found


def tree_names(raw):
    """Entry names of a raw tree object (as `git cat-file --batch` returns it)."""
    names, pos = [], 0
    while pos < len(raw):
        nul = raw.index(b"\0", pos)
        names.append(raw[raw.index(b" ", pos) + 1:nul].decode())
        pos = nul + 21  # 20-byte SHA-1 after the name
    return names


def fetch_catalog():
    """Fetch main and every dataset branch (pruning deleted ones), so dedup never works from stale refs."""
    git("fetch", "-q", "--no-tags", "--prune", "origin", "+refs/heads/main:refs/remotes/origin/main",
        f"+refs/heads/{DATASET_BRANCH_PREFIX}*:refs/remotes/origin/{DATASET_BRANCH_PREFIX}*")


def open_pr_branches():
    """Head branches of open PRs, or None without gh; dataset branches without an open PR don't count as pending."""
    try:
        out = subprocess.run(["gh", "pr", "list", "--state", "open", "--limit", "1000", "--json", "headRefName",
                              "-q", ".[].headRefName"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.split() if out.returncode == 0 else None


def main_records(ref="origin/main"):
    """{path: record} on `ref`, read from git rather than the working tree, which may be hours old."""
    paths = [p for p in git("ls-tree", "-r", "--name-only", ref, "--", "datasets/").split() if p.endswith(".yaml")]
    blobs = git_objects(f"{ref}:{p}" for p in paths)
    return {p: yaml.load(blobs[f"{ref}:{p}"][1].decode(), Loader=_NoDatesLoader) or {}
            for p in paths if f"{ref}:{p}" in blobs}


def pending_records(branches=None):
    """Records proposed on open claude/dataset/* branches but not yet on main: {branch: record}.

    `branches` (names without `origin/`) limits it to those, e.g. the head branches of open PRs; local clones
    keep remote-tracking refs of merged or closed PRs until `git fetch --prune`.
    """
    refs = git("for-each-ref", "--format=%(refname:short)", f"refs/remotes/origin/{DATASET_BRANCH_PREFIX}").split()
    if branches is not None:
        wanted = set(branches)
        refs = [r for r in refs if r.removeprefix("origin/") in wanted]
    if not refs:
        return {}
    # Files added by each branch's own commits, in one `git log` (one merge-base per branch is slow on NFS).
    # --source names the ref each commit was reached from; files deleted or moved later are missing at the tip.
    log = git("log", "--source", "--no-renames", "--diff-filter=A", "--name-only", "--format=%x00%S",
              *refs, "--not", "origin/main", "--", "datasets/")
    on_main = set(git("ls-tree", "-r", "--name-only", "origin/main", "--", "datasets/").split())  # e.g. a record moved
    added = set()
    for block in log.split("\0")[1:]:
        ref, *paths = block.split("\n")
        added.update((ref.strip(), path) for path in paths if path.endswith(".yaml") and path not in on_main)
    blobs = git_objects(f"{ref}:{path}" for ref, path in sorted(added))
    found = {}
    for ref, path in sorted(added):
        blob = blobs.get(f"{ref}:{path}")
        if blob and blob[0] == "blob":
            try:
                found[ref.removeprefix("origin/")] = yaml.load(blob[1].decode(), Loader=_NoDatesLoader) or {}
            except yaml.YAMLError:
                pass
    return found


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
    remote = git("show", f"{REJECTIONS_REF}:state/rejected.yaml")
    if remote:
        keys += (yaml.load(remote, Loader=_NoDatesLoader) or {}).get("rejected") or []
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


@functools.lru_cache(maxsize=1)
def _schema():
    return load_schema()


def canonical_path(record):
    """Where a record lives: datasets/<dimensionality>/<first modality>/<id>.yaml (None while fields are TODO)."""
    im = (record or {}).get("imaging") or {}
    dim, mods = im.get("dimensionality"), im.get("modality") or []
    imaging = _schema()["properties"]["imaging"]["properties"]
    dims, mod_enum = imaging["dimensionality"]["enum"], imaging["modality"]["items"]["enum"]
    if dim not in dims or not mods or mods[0] not in mod_enum or not record.get("id"):
        return None
    return DATASETS_DIR / dim / mods[0] / f"{record['id']}.yaml"


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
    if record.get("landing_url") and not keys:  # several datasets can share a landing page (challenge sites)
        keys.append("url:" + normalize_url(record["landing_url"]))
    return keys


_last_request = {}


def polite_get(url, min_interval=1.0, **kw):
    """requests.get with ≤ 1 request/second per host (CLAUDE.md rule 10) and our User-Agent."""
    import time

    import requests
    host = urlsplit(url).netloc
    wait = _last_request.get(host, 0) + min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request[host] = time.monotonic()
    kw.setdefault("timeout", 60)
    kw["headers"] = {"User-Agent": USER_AGENT, **(kw.get("headers") or {})}
    for attempt in range(4):  # rate-limited (e.g. parallel enricher runs): wait as told, then retry
        r = requests.get(url, **kw)
        if r.status_code not in (429, 503) or attempt == 3:
            return r
        try:
            delay = min(120, float(r.headers.get("Retry-After", 0)) or 15 * (attempt + 1))
        except ValueError:
            delay = 15 * (attempt + 1)
        time.sleep(delay)
        _last_request[host] = time.monotonic()


SIMILAR_TITLE = 0.92            # titles this similar are likely the same dataset
SIMILAR_REST_SAME_PAPER = 0.5   # same paper, and this much overlap in the title words after a shared prefix


def _title_rest_overlap(ta, tb):
    """Word overlap (Jaccard) of two titles after dropping their common leading words, so a shared
    series prefix ("Cell Tracking Challenge ...") doesn't count as similarity."""
    wa, wb = ta.split(), tb.split()
    n = 0
    while n < min(len(wa), len(wb)) and wa[n] == wb[n]:
        n += 1
    ra, rb = set(wa[n:]), set(wb[n:])
    return len(ra & rb) / len(ra | rb) if ra | rb else 1.0


def similarity_reasons(a, b):
    """Why records a and b are likely the same dataset (re-deposit, version, mirror); [] if not.

    Exact identity (same DOI / accession) is identity_keys(); this catches what those miss. Only strong
    signals count, because one paper or challenge page often releases several different datasets
    (Cell Tracking Challenge, EmbedSeg) and series reuse title templates: the same download URL, a
    >= 92% similar title, or the same paper with titles that still overlap after their shared prefix."""
    import difflib
    reasons = []
    da, db = ((r.get("data") or {}).get("download_url") for r in (a, b))
    if da and db and normalize_url(da) == normalize_url(db):
        reasons.append("same download URL")
    ia, ib = ({k for k in identity_keys(r) if not k.startswith("url:")} for r in (a, b))
    if ia and ib and not ia & ib:  # each has its own DOI / accession: different unless the download is the same
        return reasons
    ta, tb = normalize_title(a.get("title")), normalize_title(b.get("title"))
    if not (ta and tb):
        return reasons
    ratio = difflib.SequenceMatcher(None, ta, tb).ratio()
    pa = {(p.get("doi") or "").lower() for p in a.get("publications") or [] if isinstance(p, dict) and p.get("doi")}
    pb = {(p.get("doi") or "").lower() for p in b.get("publications") or [] if isinstance(p, dict) and p.get("doi")}
    if ratio >= SIMILAR_TITLE:
        reasons.append(f"title {ratio:.0%} similar")
    elif pa & pb and _title_rest_overlap(ta, tb) >= SIMILAR_REST_SAME_PAPER:
        reasons.append(f"same paper ({', '.join(sorted(pa & pb))}) and similar title")
    return reasons


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
