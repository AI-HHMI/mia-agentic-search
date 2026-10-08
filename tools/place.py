"""Move dataset records to where they belong: datasets/<dimensionality>/<first modality>/<id>.yaml.

    python tools/place.py <record.yaml> [...]       # move these files (git mv when tracked)

Run it after changing a record's dimensionality or first modality (the validator says when).
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import DATASETS_DIR, canonical_path, git, load_yaml, rel  # noqa: E402
from tools.publish import run  # noqa: E402


def move_file(path):
    path = Path(path).resolve()
    target = canonical_path(load_yaml(path))
    if target is None:
        print(f"skip {rel(path)}: dimensionality / modality not valid")
        return None
    if target == path:
        return path
    target.parent.mkdir(parents=True, exist_ok=True)
    if git("ls-files", "--error-unmatch", str(path)).strip():
        run("mv", str(path), str(target))
    else:
        shutil.move(path, target)
    for d in (path.parent, path.parent.parent):  # drop emptied folders
        if d != DATASETS_DIR and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    print(f"{rel(path)} -> {rel(target)}")
    return target


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    for p in ap.parse_args().files:
        move_file(p)


if __name__ == "__main__":
    main()
