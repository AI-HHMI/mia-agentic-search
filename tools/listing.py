"""List every file of a dataset from its host's file API and summarize it as a folder tree.

    python tools/listing.py <url> [<url> ...] [--expand-zips] [--max-lines 40] [--json]

Several URLs (e.g. the separate zips a dataset page links to) are listed together; pass all of them,
since the total only counts as confirmed when the listing covers every file of the dataset.

Supported <url>s:
  Zenodo record / DOI · Figshare article · Hugging Face dataset · S3 prefix (s3://bucket/prefix,
  virtual-host or path style, Wasabi) · CZ cryoET Data Portal dataset page · ftp:// directory ·
  HTTP directory index (Apache/nginx; ftp.ebi.ac.uk is read over FTP for exact sizes) ·
  a single .zip (lists its members) · any other single file (Content-Length).

Prints the tree, then a JSON summary line with n_files, total_bytes, complete, exact_sizes and
`size_source`: `file-listing` only when every file was listed with an exact byte size. Only that
value may go into technical.size_source as a confirmed size (see CLAUDE.md, enricher section).
"""
import argparse
import collections
import ftplib
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import JUNK_PATH, human_size, polite_get  # noqa: E402
from tools.peek_archive import ZipError, remote_size, zip_entries  # noqa: E402


class Listing:
    def __init__(self, source):
        self.source = source
        self.files = []          # (path, size or None, url or None)
        self.exact = True        # every size is an exact byte count
        self.complete = True     # nothing was skipped or truncated
        self.problems = []

    def add(self, path, size, url=None, exact=True):
        self.files.append((path, size, url))
        if size is None or not exact:
            self.exact = False


# ---------- sources ----------

def zenodo(rec_id, out):
    r = polite_get(f"https://zenodo.org/api/records/{rec_id}/files")
    r.raise_for_status()
    d = r.json()
    for e in d.get("entries", []):
        out.add(e["key"], e.get("size"), (e.get("links") or {}).get("content"))
    if d.get("entries") is None:
        out.complete = False
        out.problems.append("zenodo returned no file entries (restricted or embargoed record?)")


def figshare(article_id, out):
    page = 1
    while True:
        r = polite_get(f"https://api.figshare.com/v2/articles/{article_id}/files", params={"page": page, "page_size": 1000})
        r.raise_for_status()
        items = r.json()
        for f in items:
            out.add(f["name"], f.get("size"), f.get("download_url"))
        if len(items) < 1000:
            break
        page += 1


def huggingface(repo, out, max_files):
    url = f"https://huggingface.co/api/datasets/{repo}/tree/main?recursive=true"
    while url:
        r = polite_get(url)
        r.raise_for_status()
        for e in r.json():
            if e.get("type") == "file":
                size = (e.get("lfs") or {}).get("size", e.get("size"))
                out.add(e["path"], size, f"https://huggingface.co/datasets/{repo}/resolve/main/{e['path']}")
        url = r.links.get("next", {}).get("url")
        if url and len(out.files) >= max_files:
            out.complete = False
            out.problems.append(f"stopped after {len(out.files)} files (--max-files)")
            break


def s3(endpoint, bucket, prefix, out, max_files, virtual_host=True):
    base = f"https://{bucket}.{endpoint}/" if virtual_host else f"https://{endpoint}/{bucket}/"
    token = None
    ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        r = polite_get(base, params=params)
        if r.status_code == 301 and virtual_host:  # bucket lives in another region
            ep = re.search(r"<Endpoint>([^<]+)</Endpoint>", r.text)
            if ep:
                return s3(ep.group(1).removeprefix(f"{bucket}."), bucket, prefix, out, max_files)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for c in root.iter(f"{ns}Contents"):
            key = c.find(f"{ns}Key").text
            if not key.endswith("/"):
                out.add(key[len(prefix):] if prefix.endswith("/") else key, int(c.find(f"{ns}Size").text), base + key)
        token_el = root.find(f"{ns}NextContinuationToken")
        if token_el is None:
            break
        token = token_el.text
        if len(out.files) >= max_files:
            out.complete = False
            out.problems.append(f"stopped after {len(out.files)} files (--max-files)")
            break


def ftp_tree(host, path, out, max_files, max_dirs=400):
    ftp = ftplib.FTP(host, timeout=60)
    ftp.login()
    root = path.rstrip("/") or "/"
    todo, seen = [root], 0
    while todo:
        d = todo.pop(0)
        seen += 1
        if seen > max_dirs or len(out.files) >= max_files:
            out.complete = False
            out.problems.append(f"stopped after {seen - 1} directories / {len(out.files)} files")
            break
        try:
            entries = list(ftp.mlsd(d, facts=["type", "size"]))
            for name, facts in entries:
                if facts.get("type") == "dir":
                    todo.append(f"{d}/{name}")
                elif facts.get("type") == "file":
                    out.add(f"{d}/{name}"[len(root) + 1:], int(facts["size"]), f"ftp://{host}{d}/{name}")
        except ftplib.error_perm:  # server without MLSD: parse `LIST` lines (unix format)
            lines = []
            ftp.retrlines(f"LIST {d}", lines.append)
            for ln in lines:
                parts = ln.split(None, 8)
                if len(parts) < 9:
                    continue
                if ln.startswith("d"):
                    todo.append(f"{d}/{parts[8]}")
                elif ln.startswith("-"):
                    out.add(f"{d}/{parts[8]}"[len(root) + 1:], int(parts[4]), f"ftp://{host}{d}/{parts[8]}")
    ftp.quit()


_SIZE = re.compile(r"^\s*([\d.]+)\s*([KMGTP]?)B?\s*$", re.I)


def _parse_size(text):
    m = _SIZE.match(text or "")
    if not m:
        return None, False
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}[m.group(2).upper()]
    return int(float(m.group(1)) * mult), mult == 1


def http_index(url, out, max_files, max_dirs=200):
    """Apache/nginx autoindex. Sizes there are rounded (e.g. 14K), so exact_sizes is usually false."""
    root = url if url.endswith("/") else url + "/"
    todo, seen = [root], 0
    while todo:
        d = todo.pop(0)
        seen += 1
        if seen > max_dirs or len(out.files) >= max_files:
            out.complete = False
            out.problems.append(f"stopped after {seen - 1} directories / {len(out.files)} files")
            break
        r = polite_get(d)
        r.raise_for_status()
        for row in re.findall(r"<tr>(.*?)</tr>", r.text, re.S | re.I) or r.text.splitlines():
            m = re.search(r'<a href="([^"?#]+)"', row, re.I)
            if not m:
                continue
            href = html.unescape(m.group(1))
            target = urljoin(d, href)
            if not target.startswith(d) or target == d:  # parent directory / sort links / external
                continue
            rel = unquote(target[len(root):])
            if href.endswith("/"):
                todo.append(target)
                continue
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
            size, exact = None, False
            for c in reversed(cells):  # Apache: size cell right after the date cell
                size, exact = _parse_size(re.sub(r"<[^>]+>|&nbsp;", "", c))
                if size is not None:
                    break
            if size is None:  # nginx: "<a ...>name</a>   date   12345"
                tail = re.search(r"</a>\s+\S+\s+\S+\s+(\d+|-)\s*$", row)
                size, exact = (int(tail.group(1)), True) if tail and tail.group(1) != "-" else (None, False)
            out.add(rel, size, target, exact)


def single_file(url, out):
    size = remote_size(url)
    out.add(urlsplit(url).path.rsplit("/", 1)[-1], size or None, url)


def expand_zips(out, max_zips=20):
    zips = [(p, u) for p, _, u in out.files if p.lower().endswith(".zip") and u and u.startswith("http")]
    for path, url in zips[:max_zips]:
        try:
            _, entries = zip_entries(url)
        except ZipError as e:
            out.problems.append(f"{path}: {e}")
            continue
        for e in entries:
            out.members.append((f"{path}/{e['name']}", e["size"]))
    if len(zips) > max_zips:
        out.problems.append(f"only the first {max_zips} of {len(zips)} zips were expanded")


def dispatch(url, out_args):
    u = urlsplit(url)
    host, path = u.netloc.lower(), u.path
    m = re.search(r"zenodo\.org/(?:api/)?records?/(\d+)", url) or re.search(r"10\.5281/zenodo\.(\d+)", url)
    if m and "/files/" not in path:
        out = Listing("zenodo")
        zenodo(m.group(1), out)
        return out
    m = re.search(r"figshare\.com/(?:articles/(?:[^/]+/)*|v2/articles/)(\d+)", url)
    if m and "ndownloader" not in host:
        out = Listing("figshare")
        figshare(m.group(1), out)
        return out
    m = re.match(r"/datasets/([^/]+/[^/]+)", path)
    if host == "huggingface.co" and m and "/resolve/" not in path:
        out = Listing("huggingface")
        huggingface(m.group(1), out, out_args.max_files)
        return out
    m = re.match(r"/datasets/(\d+)", path)
    if host == "cryoetdataportal.czscience.com" and m:
        out = Listing("s3 (cryoet-data-portal-public)")
        s3("s3.amazonaws.com", "cryoet-data-portal-public", f"{m.group(1)}/", out, out_args.max_files)
        return out
    if u.scheme == "s3":
        out = Listing("s3")
        s3("s3.amazonaws.com", host, path.lstrip("/"), out, out_args.max_files)
        return out
    m = re.match(r"^([^.]+)\.(s3[.-][^/]*amazonaws\.com)$", host)
    if m and not path.lower().endswith(".zip"):
        out = Listing("s3")
        s3(m.group(2), m.group(1), path.lstrip("/"), out, out_args.max_files)
        return out
    if re.match(r"^s3[.-].*(amazonaws|wasabisys)\.com$", host) and not path.lower().endswith((".zip", ".tgz", ".tar", ".gz")):
        bucket, _, prefix = path.lstrip("/").partition("/")
        out = Listing("s3")
        s3(host, bucket, prefix, out, out_args.max_files, virtual_host=False)
        return out
    is_file = re.search(r"\.[A-Za-z0-9]{1,5}$", path)
    if u.scheme == "ftp" or (host == "ftp.ebi.ac.uk" and not is_file):
        out = Listing("ftp")
        try:
            ftp_tree(host, unquote(path), out, out_args.max_files)
            return out
        except (OSError, ftplib.Error) as e:  # sandbox may block FTP; fall back to the HTTP index
            if u.scheme == "ftp":
                raise
            print(f"note: FTP failed ({e}); reading the HTTP index instead (rounded sizes)", file=sys.stderr)
    if path.lower().endswith(".zip"):
        out = Listing("zip")
        size, entries = zip_entries(url)
        name = path.rsplit("/", 1)[-1]
        out.add(name, size, url)
        out.members = [(f"{name}/{e['name']}", e["size"]) for e in entries]
        return out
    if not is_file:
        r = polite_get(url)
        if "text/html" in r.headers.get("Content-Type", "") and re.search(r"Index of|<pre>|Parent Directory", r.text, re.I):
            out = Listing("http-index")
            http_index(url, out, out_args.max_files)
            return out
    out = Listing("single-file")
    single_file(url, out)
    return out


# ---------- tree ----------

def _ext(name):
    base = name.rsplit("/", 1)[-1].lower()
    for multi in (".ome.tiff", ".ome.tif", ".ome.zarr", ".nii.gz", ".tar.gz"):
        if base.endswith(multi):
            return multi[1:]
    return base.rsplit(".", 1)[-1] if "." in base else "(none)"


def _collapse(paths):
    """Replace digit runs in directory names by '#' where ≥ 3 siblings share the pattern."""
    parts = [p.split("/")[:-1] for p in paths]
    depth = max((len(p) for p in parts), default=0)
    for level in range(depth):
        groups = collections.defaultdict(set)
        for p in parts:
            if len(p) > level:
                groups[(tuple(p[:level]), re.sub(r"\d+", "#", p[level]))].add(p[level])
        for p in parts:
            if len(p) > level:
                key = (tuple(p[:level]), re.sub(r"\d+", "#", p[level]))
                if len(groups[key]) >= 3:
                    p[level] = key[1]
    return ["/".join(p) for p in parts]


_STORE = re.compile(r"^(.*?\.(?:zarr|n5))/", re.I)


def tree(files, max_lines):
    """files: [(path, size)]. One line per (collapsed) directory: file count, size, extensions.
    Chunk files inside a .zarr / .n5 store are counted on one line for the store."""
    n_all = len(files)
    files = [(p, s) for p, s in files if not JUNK_PATH.search(p)]
    hidden = n_all - len(files)
    files = [((m.group(1) + "/(store)." + m.group(1).rsplit(".", 1)[-1].lower()) if (m := _STORE.match(p)) else p, s)
             for p, s in files]
    prefix = ""
    split = [p.split("/")[:-1] for p, _ in files]
    while split and all(len(d) > 0 for d in split) and len({d[0] for d in split}) == 1:
        prefix += split[0][0] + "/"
        split = [d[1:] for d in split]
    files = [(p[len(prefix):], s) for p, s in files]
    dirs = _collapse([p for p, _ in files])
    orig_dirs = collections.defaultdict(set)
    stats = collections.defaultdict(lambda: {"n": 0, "bytes": 0, "ext": collections.Counter(), "unknown": False})
    for (path, size), d in zip(files, dirs):
        s = stats[d]
        s["n"] += 1
        s["bytes"] += size or 0
        s["unknown"] |= size is None
        s["ext"][_ext(path)] += 1
        orig_dirs[d].add(path.rsplit("/", 1)[0] if "/" in path else "")
    lines = []
    for d in sorted(stats):
        s = stats[d]
        mult = f" (×{len(orig_dirs[d])} dirs)" if len(orig_dirs[d]) > 1 else ""
        exts = ", ".join(f".{e} {c}" if len(s["ext"]) > 1 else f".{e}" for e, c in s["ext"].most_common(4))
        size = human_size(s["bytes"], "?") + ("+?" if s["unknown"] else "")
        lines.append(f"{(d or '.') + '/':<40}{mult:<12} {s['n']:>6} files  {size:>10}  {exts}")
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [f"… {len(lines) - max_lines + 1} more directories"]
    if hidden:
        lines.append(f"(+{hidden} version-control / OS metadata files not shown)")
    return (f"{prefix}\n" if prefix else "") + "\n".join(("  " if prefix else "") + ln for ln in lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+", metavar="url")
    ap.add_argument("--expand-zips", action="store_true", help="also list the members of each .zip (range requests)")
    ap.add_argument("--max-lines", type=int, default=40)
    ap.add_argument("--max-files", type=int, default=50000)
    ap.add_argument("--examples", type=int, default=0, help="also print N example paths per directory")
    ap.add_argument("--json", action="store_true", help="print every file as JSON lines instead of the tree")
    a = ap.parse_args()

    parts = []
    for url in a.urls:
        try:
            part = dispatch(url, a)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"listing failed for {url}: {type(e).__name__}: {e}")
        if not hasattr(part, "members"):
            part.members = []
            if a.expand_zips:
                expand_zips(part)
        parts.append(part)
    out = parts[0]
    if len(parts) > 1:  # merge; single files keep their names, directories/records get a prefix
        out = Listing(" + ".join(dict.fromkeys(p.source for p in parts)))
        out.members = []
        for url, part in zip(a.urls, parts):
            pre = "" if part.source in ("zip", "single-file") else urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] + "/"
            for path, size, u in part.files:
                out.add(pre + path, size, u)
            out.members += [(pre + m, s) for m, s in part.members]
            out.exact &= part.exact
            out.complete &= part.complete
            out.problems += part.problems

    total = sum(s or 0 for _, s, _ in out.files)
    confirmed = out.complete and out.exact and bool(out.files)
    summary = {"source": out.source, "n_files": len(out.files), "total_bytes": total, "total_human": human_size(total),
               "complete": out.complete, "exact_sizes": out.exact,
               "size_source": "file-listing" if confirmed else "estimated",
               "zip_members": len(out.members), "problems": out.problems}
    if a.json:
        for p, s, u in out.files:
            print(json.dumps({"path": p, "size": s, "url": u}))
        for p, s in out.members:
            print(json.dumps({"path": p, "size": s, "in_zip": True}))
    else:
        print(tree([(p, s) for p, s, _ in out.files], a.max_lines))
        if out.members:
            print("\nzip contents:")
            print(tree(out.members, a.max_lines))
        if a.examples:
            by_dir = collections.defaultdict(list)
            for p in [f[0] for f in out.files] + [m[0] for m in out.members]:
                if not JUNK_PATH.search(p):
                    by_dir[p.rsplit("/", 1)[0] if "/" in p else "."].append(p)
            print("\nexamples:")
            for d in sorted(by_dir)[:a.max_lines]:
                for p in by_dir[d][:a.examples]:
                    print(f"  {p}")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
