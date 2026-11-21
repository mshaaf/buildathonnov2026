"""Lab: per-test CPU measured inside a whole-class batch; class setup = session total - bodies."""
import json, resource, select, sys
sys.path.insert(0, "/app/rwx")
from engine import Engine
tests = json.load(open(sys.argv[1]))["tests"]
out = sys.argv[2]; shard, nsh = int(sys.argv[3]), int(sys.argv[4])
eng = Engine("/sqlglot", tests)
classes = {}
for t in tests:
    classes.setdefault(t["id"].rsplit("::", 1)[0], []).append(t["number"])
res = {"body": {}, "setup": {}}
for i, (cls, nums) in enumerate(sorted(classes.items())):
    if i % nsh != shard: continue
    b = resource.getrusage(resource.RUSAGE_CHILDREN)
    s = eng.session({}, {})
    s.submit(nums, timeout=900)
    while True:
        select.select([s], [], [], 1)
        if s.read(): break
    s.close()
    a = resource.getrusage(resource.RUSAGE_CHILDREN)
    total = (a.ru_utime + a.ru_stime) - (b.ru_utime + b.ru_stime)
    bodies = sum(s.cpu.get(n, 0) for n in nums)
    for n in nums: res["body"][n] = round(s.cpu.get(n, 0), 4)
    res["setup"][cls] = round(max(0.0, total - bodies), 4)
json.dump(res, open(out, "w"))
