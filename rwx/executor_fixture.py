"""Defer the pinned TestExecutor class fixture until a test reads its state."""

import json
import sys
from pathlib import Path


def build(root):
    from tests.test_executor import TestExecutor

    prefix = str(root) + "/"
    dependencies = set()

    def entered(frame, event, arg):
        if event == "call" and frame.f_code.co_filename.startswith(prefix):
            dependencies.add(frame.f_code.co_filename[len(prefix):])

    before = set(vars(TestExecutor))
    sys.setprofile(entered)
    try:
        TestExecutor.setUpClass()
        try:
            fields = sorted(vars(TestExecutor).keys() - before)
        finally:
            TestExecutor.tearDownClass()
    finally:
        sys.setprofile(None)
    (root / ".rwx-executor.json").write_text(json.dumps({"sources": sorted(dependencies), "fields": fields}))


def defer_setup(cls, fields):
    setup, teardown = cls.setUpClass, cls.tearDownClass
    initialized = False

    class Field:
        def __init__(self, name):
            self.name = name

        def __get__(self, obj, owner):
            nonlocal initialized
            setup()
            initialized = True
            return vars(cls)[self.name]

    def start(cls):
        nonlocal initialized
        initialized = False
        for name in fields:
            setattr(cls, name, Field(name))

    def finish(cls):
        if initialized:
            teardown()
        else:
            for name in fields:
                delattr(cls, name)

    cls.setUpClass = classmethod(start)
    cls.tearDownClass = classmethod(finish)


def install(root, changes):
    manifest = Path(root) / ".rwx-executor.json"
    if not manifest.is_file():
        return
    profile = json.loads(manifest.read_text())
    if changes.keys().isdisjoint(profile["sources"]):
        from tests.test_executor import TestExecutor

        defer_setup(TestExecutor, profile["fields"])


if __name__ == "__main__":
    build(Path(sys.argv[1]).resolve())
