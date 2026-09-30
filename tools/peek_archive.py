"""List the files inside a remote .zip without downloading it (HTTP range requests, ~100 KB).

    python tools/peek_archive.py <zip url> [--max 40]
    python tools/peek_archive.py <zip url> --cat <path in zip>    # print a text member (README, LICENSE; ≤ 200 KB)

Use it to fill `data.formats` / `annotations.format` instead of writing `other` for archives.
Prints the file count, extensions with counts and total sizes, and the first --max paths.
Only .zip is supported (tar/tar.gz have no index to read remotely).

zip_entries() and read_member() are reused by tools/listing.py, probe.py and sample.py.
"""
import argparse
import collections
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import USER_AGENT, polite_get  # noqa: E402

HEADERS = {"User-Agent": USER_AGENT}
TAIL = 128 * 1024


class ZipError(Exception):
    pass


def fetch_range(url, start, end):
    r = polite_get(url, headers={**HEADERS, "Range": f"bytes={start}-{end}"}, allow_redirects=True)
    if r.status_code != 206:
        raise ZipError(f"server does not support range requests (HTTP {r.status_code}); can't peek without downloading")
    return r.content


def remote_size(url):
    import requests
    head = requests.head(url, headers=HEADERS, timeout=60, allow_redirects=True)
    size = int(head.headers.get("Content-Length", 0))
    if not size:  # some servers omit Content-Length on HEAD; a 1-byte range reports the total
        r = polite_get(url, headers={"Range": "bytes=0-0"}, allow_redirects=True)
        size = int(r.headers.get("Content-Range", "/0").rsplit("/", 1)[-1] or 0)
    return size


def zip_entries(url):
    """Central directory of a remote zip: (archive size, [dict(name, size, csize, method, offset)])."""
    size = remote_size(url)
    if not size:
        raise ZipError("server did not report a file size; can't locate the zip index")
    tail = fetch_range(url, max(0, size - TAIL), size - 1)
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise ZipError("no zip end-of-central-directory record found; not a zip file?")
    n, cd_size, cd_off = struct.unpack("<HII", tail[eocd + 10:eocd + 20])
    if cd_off == 0xFFFFFFFF or n == 0xFFFF:  # zip64
        loc = tail.rfind(b"PK\x06\x07")
        z64_off = struct.unpack("<Q", tail[loc + 8:loc + 16])[0]
        rec = fetch_range(url, z64_off, z64_off + 55)
        n, cd_size, cd_off = struct.unpack("<QQQ", rec[32:56])
    cd = fetch_range(url, cd_off, cd_off + cd_size - 1)

    files, i = [], 0
    while i + 46 <= len(cd) and cd[i:i + 4] == b"PK\x01\x02":
        method = struct.unpack("<H", cd[i + 10:i + 12])[0]
        csize, usize = struct.unpack("<II", cd[i + 20:i + 28])
        ln, le, lc = struct.unpack("<HHH", cd[i + 28:i + 34])
        offset = struct.unpack("<I", cd[i + 42:i + 46])[0]
        name = cd[i + 46:i + 46 + ln].decode("utf-8", "replace")
        extra = cd[i + 46 + ln:i + 46 + ln + le]
        if 0xFFFFFFFF in (usize, csize, offset):  # zip64 extra field holds the real values, in this order
            j = 0
            while j + 4 <= len(extra):
                tag, sz = struct.unpack("<HH", extra[j:j + 4])
                if tag == 1:
                    vals, k = list(struct.unpack(f"<{sz // 8}Q", extra[j + 4:j + 4 + sz // 8 * 8])), 0
                    if usize == 0xFFFFFFFF and k < len(vals):
                        usize, k = vals[k], k + 1
                    if csize == 0xFFFFFFFF and k < len(vals):
                        csize, k = vals[k], k + 1
                    if offset == 0xFFFFFFFF and k < len(vals):
                        offset = vals[k]
                    break
                j += 4 + sz
        if not name.endswith("/") and "__MACOSX/" not in name:  # skip macOS resource forks
            files.append({"name": name, "size": usize, "csize": csize, "method": method, "offset": offset})
        i += 46 + ln + le + lc
    return size, files


def read_member(url, entry, max_bytes=None):
    """Bytes of one zip member (stored or deflated), read with range requests.

    With max_bytes, only the first max_bytes of the *uncompressed* member are returned
    (enough for headers of most formats); returns (data, complete)."""
    local = fetch_range(url, entry["offset"], entry["offset"] + 29)
    ln, le = struct.unpack("<HH", local[26:30])
    start = entry["offset"] + 30 + ln + le
    # deflate rarely expands data, so max_bytes of compressed input yields ≥ max_bytes of output
    want = entry["csize"] if max_bytes is None else min(entry["csize"], max_bytes)
    raw = fetch_range(url, start, start + want - 1) if want else b""
    if entry["method"] == 0:
        return raw, want == entry["csize"]
    if entry["method"] == 8:
        d = zlib.decompressobj(-15)
        out = d.decompress(raw, max_bytes or 0)
        return out, (want == entry["csize"] and (max_bytes is None or len(out) < max_bytes))
    raise ZipError(f"unsupported zip compression method {entry['method']} for {entry['name']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--cat", metavar="MEMBER", help="print this text member instead of the listing")
    a = ap.parse_args()
    try:
        size, entries = zip_entries(a.url)
        if a.cat:
            entry = next((e for e in entries if e["name"] == a.cat), None)
            if entry is None:
                sys.exit(f"{a.cat} not in the zip")
            data, complete = read_member(a.url, entry, 200 * 1024)
            print(data.decode("utf-8", "replace") + ("" if complete else "\n[… truncated at 200 KB]"))
            return
    except ZipError as e:
        sys.exit(str(e))
    files = [(e["name"], e["size"]) for e in entries]

    by_ext = collections.defaultdict(lambda: [0, 0])
    for name, s in files:
        base = name.rsplit("/", 1)[-1].lower()
        ext = ".".join(base.split(".")[1:][-2:]) if "." in base else "(none)"
        by_ext[ext][0] += 1
        by_ext[ext][1] += s
    print(f"archive: {size / 1e6:.1f} MB compressed · {len(files)} files · "
          f"{sum(s for _, s in files) / 1e6:.1f} MB uncompressed")
    print("extensions:")
    for ext, (count, total) in sorted(by_ext.items(), key=lambda kv: -kv[1][0]):
        print(f"  .{ext:<14} {count:>6} files  {total / 1e6:>10.1f} MB")
    print(f"first {min(a.max, len(files))} paths:")
    for name, s in files[: a.max]:
        print(f"  {name}  ({s / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
