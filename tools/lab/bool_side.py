"""Write the and/or value profiles as a side file next to the coverage map (lab only).

    python tools/lab/merge_bool.py rwx/maps/cov.pkl.z BOOL_DIR tests.json /tmp/map_bool.pkl.z
    python tools/lab/bool_side.py /tmp/map_bool.pkl.z rwx/maps/bool.pkl.z

The side file holds {"bool_sites": {file: {(line, col): (op, mask)}}, "bool_unprofiled": mask}
with masks inline, so the main map stays untouched. selector.Map loads it only when
RWX_BOOL_PROFILE=1 and the main map has no bool_sites of its own.
"""
import pickle
import sys
import zlib


def main():
    src, dst = sys.argv[1:3]
    d = pickle.loads(zlib.decompress(open(src, "rb").read()))
    masks = d["masks"]
    sites = {f: {k: (op, masks[mid]) for k, (op, mid) in v.items()} for f, v in d["bool_sites"].items()}
    out = {"bool_sites": sites, "bool_unprofiled": masks[d["bool_unprofiled"]]}
    blob = zlib.compress(pickle.dumps(out, protocol=4), 9)
    open(dst, "wb").write(blob)
    print(f"{sum(len(v) for v in sites.values())} sites in {len(sites)} files, {len(blob)} bytes")


if __name__ == "__main__":
    main()
