"""Can TensorSwitch download this? Checks a URL (or `<zip url>::<member>`) with one small range request.

    python tools/download_check.py <url> [<url> ...] --id ID
    python tools/download_check.py '<zip url>::<path in zip>' --id ID
    python tools/download_check.py <url> --tensorswitch ../tensorswitch/src --id ID   # use its live host allowlist

TensorSwitch's fetch_dataset takes http(s), ftp and s3 URLs from an allowlist of hosts, downloads a file
(never a web page), and reads a single member of a remote zip with range requests. Per URL this prints one
JSON object: final URL after redirects, HTTP status, content type, size, range support, whether it is a
zip, and `usable` with the reasons when it isn't. At most ~1 KB per file is read (plus a zip's index for
`::` specs). Each check is recorded in the active run log, so a usable URL can be cited as evidence.
"""
import argparse
import ftplib
import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import polite_get  # noqa: E402
from tools.peek_archive import zip_entries  # noqa: E402
from tools.run_log import log_inspection  # noqa: E402

SCHEMES = ("http", "https", "ftp", "s3")
# copy of tensorswitch_v2/utils/fetch.py DEFAULT_ALLOWED_HOSTS (unified, 008b5a7); --tensorswitch reads the live list
ALLOWED_HOSTS = (
    "zenodo.org", "ftp.ebi.ac.uk", "www.ebi.ac.uk", "data.broadinstitute.org",
    "data.celltrackingchallenge.net", "ndownloader.figshare.com", "figshare.com",
    "huggingface.co", "github.com", "raw.githubusercontent.com", "s3.amazonaws.com",
    "datasets.gryf.fi.muni.cz", "rgw.cscs.ch", "files.cryoetdataportal.cziscience.com",
    "dataverse.harvard.edu", "data.mendeley.com", "datadryad.org", "osf.io",
    "bossdb-open-data.s3.amazonaws.com", "janelia-cosem-datasets.s3.amazonaws.com",
)


def ts_allowed_hosts(src):
    spec = importlib.util.spec_from_file_location("ts_fetch", Path(src) / "tensorswitch_v2" / "utils" / "fetch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return tuple(mod.allowed_hosts())


def https_form(url):
    """s3://bucket/key -> its public HTTPS URL (as TensorSwitch does)."""
    p = urlsplit(url)
    return f"https://{p.netloc}.s3.amazonaws.com{p.path}" if p.scheme == "s3" else url


def check(spec, hosts):
    url, _, member = spec.partition("::")
    res = {"spec": spec, "url": url, "member": member or None, "usable": False, "problems": []}
    scheme = urlsplit(url).scheme.lower()
    if scheme not in SCHEMES:
        res["problems"].append(f"scheme {scheme or '?'!r} not supported (http, https, ftp, s3)")
        return res
    url = https_form(url)
    host = (urlsplit(url).hostname or "").lower()
    res["host_allowed"] = any(host == h or host.endswith("." + h) for h in hosts)
    if not res["host_allowed"]:
        res["problems"].append(f"host {host} is not on TensorSwitch's fetch allowlist (TENSORSWITCH_FETCH_HOSTS={host} "
                               "allows it for one run; ask for it to be added)")
    if scheme == "ftp":
        try:
            ftp = ftplib.FTP(host, timeout=60)
            ftp.login()
            ftp.voidcmd("TYPE I")
            res["size_bytes"] = ftp.size(urlsplit(url).path)
            ftp.quit()
            res["is_file"] = True
        except ftplib.all_errors as e:
            res["problems"].append(f"FTP: {e} (a folder, or the file doesn't exist)")
            res["is_file"] = False
        if member:
            res["problems"].append("zip members can't be read over FTP; use the https:// form of the same path if the host has one")
    else:
        try:
            r = polite_get(url, headers={"Range": "bytes=0-1023"}, stream=True, allow_redirects=True)
            head = next(r.iter_content(1024), b"")
            r.close()
        except Exception as e:
            res["problems"].append(f"request failed: {e}")
            return res
        ctype = r.headers.get("Content-Type", "")
        total = r.headers.get("Content-Range", "").rsplit("/", 1)[-1]
        res.update(final_url=r.url, status=r.status_code, content_type=ctype or None,
                   range_requests=r.status_code == 206,
                   size_bytes=int(total) if total.isdigit() else (int(r.headers["Content-Length"])
                                                                  if r.status_code == 200 and r.headers.get("Content-Length") else None),
                   is_zip=head.startswith(b"PK\x03\x04"))
        looks_html = "html" in ctype.lower() or head.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))
        res["is_file"] = r.status_code in (200, 206) and not looks_html
        if r.status_code not in (200, 206):
            res["problems"].append(f"HTTP {r.status_code}")
        elif looks_html:
            res["problems"].append("returns a web page, not a file (landing page, folder index or login)")
        if res["is_file"] and not res["range_requests"]:
            res["problems"].append("no range requests: only whole-file downloads, no resuming, no zip members")
        if member:
            if not res.get("is_zip"):
                res["problems"].append("`::member` given but the URL is not a zip")
            else:
                try:
                    _, files = zip_entries(url)
                    hit = next((f for f in files if f["name"] == member), None)
                    if hit is None:
                        res["problems"].append(f"no member {member!r} in the zip")
                    else:
                        res["member_size_bytes"] = hit["size"]
                except Exception as e:
                    res["problems"].append(f"zip index not readable: {e}")
    blocking = [p for p in res["problems"] if not p.startswith("no range requests") or member]
    res["usable"] = bool(res.get("is_file")) and res.get("host_allowed") and not blocking
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("specs", nargs="+")
    ap.add_argument("--id", dest="record_id")
    ap.add_argument("--tensorswitch", metavar="SRC", help="TensorSwitch src/ folder: use its current host allowlist")
    a = ap.parse_args()
    hosts = ALLOWED_HOSTS
    if a.tensorswitch:
        try:
            hosts = ts_allowed_hosts(a.tensorswitch)
        except Exception as e:
            print(f"note: TensorSwitch allowlist not loaded ({e}); using the built-in copy", file=sys.stderr)
    for spec in a.specs:
        res = check(spec.strip(), hosts)
        log_inspection("link", res["url"], a.record_id, usable=res["usable"], size_bytes=res.get("size_bytes"))
        print(json.dumps(res))


if __name__ == "__main__":
    main()
