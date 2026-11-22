"""Import hook serving patched sources in place of the files on disk.

The engine installs it in its children. A test that starts a fresh interpreter
(`python -c ...`, test_lazy_load) gets it too: the child rewrites the command to load
this module first, with the patched sources in a temporary JSON file. This module must
not import sqlglot itself.
"""

import importlib.abc
import importlib.util
import json
import sys

PKG_PREFIXES = ("sqlglot", "tests")


class PatchedFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, sources: dict, root: str):
        self.by_path = sources  # absolute path -> source
        self.root = root

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(PKG_PREFIXES):
            return None
        rel = fullname.replace(".", "/")
        for cand, is_pkg in ((f"{self.root}/{rel}.py", False), (f"{self.root}/{rel}/__init__.py", True)):
            if cand in self.by_path:
                spec = importlib.util.spec_from_file_location(
                    fullname, cand, loader=self, submodule_search_locations=[cand.rsplit("/", 1)[0]] if is_pkg else None
                )
                return spec
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = module.__spec__.origin
        code = compile(self.by_path[path], path, "exec", dont_inherit=True)
        module.__file__ = path
        exec(code, module.__dict__)


def install(sources_path: str, root: str):
    with open(sources_path, encoding="utf-8") as fh:
        sources = json.load(fh)
    sys.meta_path.insert(0, PatchedFinder(sources, root))


def wrap_subprocess(sources: dict, root: str, here: str, tmp_dir: str = "/tmp"):
    """Make `python -c CODE` children of this process see the patched sources."""
    import os
    import subprocess

    real_run = subprocess.run

    def run(args, *a, **k):
        if (sources and isinstance(args, (list, tuple)) and len(args) == 3
                and args[0] in ("python", sys.executable) and args[1] == "-c" and isinstance(args[2], str)):
            path = f"{tmp_dir}/rwx-src-{os.getpid()}.json"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(sources, fh)
            hook = f"import sys; sys.path.append({here!r}); import srchook; srchook.install({path!r}, {root!r})\n"
            try:
                return real_run([args[0], "-c", hook + args[2]], *a, **k)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return real_run(args, *a, **k)

    subprocess.run = run
