"""Lab: real CPU cost of running each test alone in a fresh engine session (class setup included)."""
import json, os, resource, sys, time
sys.path.insert(0, "/app/rwx")
from engine import Engine
tests = json.load(open(sys.argv[1]))["tests"]
out = sys.argv[2]; shard, nsh = int(sys.argv[3]), int(sys.argv[4])
eng = Engine("/sqlglot", tests)
res = {}
for t in tests:
    if t["number"] % nsh != shard: continue
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    s = eng.session({}, {})
    s.submit([t["number"]], timeout=600)
    import select
    while True:
        select.select([s], [], [], 1)
        if s.read(): break
    st = s.results.get(t["number"])
    s.close()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    res[t["number"]] = [round((after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime), 4), st]
json.dump(res, open(out, "w"))
