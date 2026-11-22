"""Lab: which setUpClass-loaded fixture attributes each test actually reads.

    python /app/tools/lab/attrrun.py tests.json OUT.json
For every class with setup-loaded fixtures (see build_map.build_fixture_attrs), each of
its tests runs in a fresh fork; after setUpClass the attributes are wrapped in list
proxies that record any read."""
import importlib, json, os, sys, unittest
sys.path.insert(0, "/app/rwx"); sys.path.insert(0, "/app/tools")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
for m in ("duckdb", "pandas", "numpy"):
    importlib.import_module(m)
from engine import install_inline_pool
from build_map import build_fixture_attrs

tests = json.load(open(sys.argv[1]))["tests"]
attrs, _ = build_fixture_attrs("/sqlglot")
by_class = {}
for fx, pairs in attrs.items():
    for class_id, attr in pairs:
        by_class.setdefault(class_id, set()).add(attr)
result = {}
for t in tests:
    class_id = t["id"].rsplit("::", 1)[0]
    if class_id not in by_class:
        continue
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        read = set()
        install_inline_pool()
        path, cls_name, meth = t["id"].split("::")
        mod = importlib.import_module(path[:-3].replace("/", "."))
        klass = getattr(mod, cls_name)
        orig = klass.setUpClass.__func__

        def setup(cls):
            orig(cls)
            for a in by_class[class_id]:
                class Tracked(list):
                    def __getitem__(self, i, _a=a):
                        read.add(_a); return list.__getitem__(self, i)
                    def __iter__(self, _a=a):
                        read.add(_a); return list.__iter__(self)
                    def __len__(self, _a=a):
                        read.add(_a); return list.__len__(self)
                setattr(cls, a, Tracked(getattr(cls, a)))

        klass.setUpClass = classmethod(setup)
        unittest.TestSuite([unittest.TestSuite([klass(meth)])]).run(unittest.TestResult())
        os.write(w, json.dumps(sorted(read)).encode())
        os._exit(0)
    os.close(w)
    buf = b""
    while True:
        c = os.read(r, 65536)
        if not c:
            break
        buf += c
    os.waitpid(pid, 0)
    result[t["number"]] = json.loads(buf or b"[]")
json.dump(result, open(sys.argv[2], "w"))
print({k: v for k, v in result.items() if v}, file=sys.stderr)
