"""Run TestExecutor's tests on the edited queries of a setup-loaded fixture only.

TestExecutor.setUpClass loads tpc-h.sql / tpc-ds.sql into cls.tpch_sqls / cls.tpcds_sqls.
In a session for a fixture-only commit the parent's copy is stale, so every test method
is wrapped to see the edited file's pairs instead. The per-query tests (each query is
checked on its own) see only the pairs that differ from the pinned file: the others are
byte-identical, with no code changed, and pass as they do at the pinned commit.

If the edited file does not pair up with the pinned one query by query, every wrapped
test fails, so the selector keeps them all.
"""

PER_QUERY = {
    "test_execute_tpch": "tpch_sqls",
    "test_optimized_tpch": "tpch_sqls",
    "test_execute_tpcds": "tpcds_sqls",
}


def load(spec):
    """Pairs as the real loader reads them now (call before and after patching open)."""
    from tests.helpers import load_sql_fixture_pairs

    return {attr: list(load_sql_fixture_pairs(fixture)) for fixture, attr in spec}


def install(old, new):
    from tests.test_executor import TestExecutor

    views = {}
    for attr, pairs in new.items():
        before = old.get(attr)
        if before is None or len(before) != len(pairs):
            views[attr] = None
        else:
            views[attr] = (pairs, [p for p, q in zip(pairs, before) if p != q])

    for name in [n for n in vars(TestExecutor) if n.startswith("test")]:
        original = getattr(TestExecutor, name)
        if not callable(original):
            continue

        def wrapped(self, _original=original, _name=name):
            for attr, view in views.items():
                if view is None:
                    raise AssertionError(f"{attr}: edited fixture does not pair up with the pinned one")
                pairs, edited = view
                setattr(self, attr, edited if PER_QUERY.get(_name) == attr else pairs)
            try:
                return _original(self)
            finally:
                for attr in views:
                    self.__dict__.pop(attr, None)

        wrapped.__name__ = name
        setattr(TestExecutor, name, wrapped)
