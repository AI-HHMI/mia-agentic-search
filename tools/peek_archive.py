"""List the files inside a remote .zip without downloading it (HTTP range requests, ~100 KB).

    python tools/peek_archive.py <zip url> [--max 40]

Use it to fill `data.formats` / `annotations.format` instead of writing `other` for archives.
Prints the file count, extensions with counts and total sizes, and the first --max paths.
Only .zip is supported (tar/tar.gz have no index to read remotely).
"""
import argparse
import collections
import struct
import sys

import requests

HEADERS = {"User-Agent": "mia-agentic-search/1.0 (archive listing)"}
TAIL = 128 * 1024


def fetch_range(url, start, end):
    r = requests.get(url, headers={**HEADERS, "Range": f"bytes={start}-{end}"}, timeout=60, allow_redirects=True)
    if r.status_code != 206:
        sys.exit(f"server does not support range requests (HTTP {r.status_code}); can't peek without downloading")
    return r.content


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--max", type=int, default=40)
    a = ap.parse_args()

    head = requests.head(a.url, headers=HEADERS, timeout=60, allow_redirects=True)
    size = int(head.headers.get("Content-Length", 0))
    if not size:
        sys.exit("server did not report a file size; can't locate the zip index")
    tail = fetch_range(a.url, max(0, size - TAIL), size - 1)
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        sys.exit("no zip end-of-central-directory record found; not a zip file?")
    n, cd_size, cd_off = struct.unpack("<HII", tail[eocd + 10:eocd + 20])
    if cd_off == 0xFFFFFFFF or n == 0xFFFF:  # zip64
        loc = tail.rfind(b"PK\x06\x07")
        z64_off = struct.unpack("<Q", tail[loc + 8:loc + 16])[0]
        rec = fetch_range(a.url, z64_off, z64_off + 55)
        n, cd_size, cd_off = struct.unpack("<QQQ", rec[32:56])
    cd = fetch_range(a.url, cd_off, cd_off + cd_size - 1)

    files, i = [], 0
    while i + 46 <= len(cd) and cd[i:i + 4] == b"PK\x01\x02":
        usize = struct.unpack("<I", cd[i + 24:i + 28])[0]
        ln, le, lc = struct.unpack("<HHH", cd[i + 28:i + 34])
        name = cd[i + 46:i + 46 + ln].decode("utf-8", "replace")
        if usize == 0xFFFFFFFF and le:  # zip64 extra field holds the real size
            extra = cd[i + 46 + ln:i + 46 + ln + le]
            if extra[:2] == b"\x01\x00":
                usize = struct.unpack("<Q", extra[4:12])[0]
        if not name.endswith("/") and "__MACOSX/" not in name:  # skip macOS resource forks
            files.append((name, usize))
        i += 46 + ln + le + lc

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
