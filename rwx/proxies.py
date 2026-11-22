"""Cheap stand-ins for tests too slow to run, used only to decide whether to print them.

test_max_nodes (28.8 s recorded, 34 s CPU) parses 10,000 columns of `x=x=...=x` (depth
100) to hit the 1,000,000 node limit, then the 50,000 limit. The stand-in runs every
check of the test with 100 columns and both limits scaled by the same factor (10,000 and
500): the same parser paths, the same ratio of nodes to limit, the same messages. The
cheap checks (SELECT 1, 500 columns, the max_nodes=5 parse) run unchanged.
"""


def max_nodes_standin(self):
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    self.assertIsInstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)

    cols = [f"col{i}" for i in range(500)]
    sql = f"SELECT {','.join(cols)} FROM t"
    self.assertIsInstance(parse_one(sql, max_nodes=1_000_000), exp.Select)

    width = 100  # the test: 10000
    depth = 100
    expr = "x" + "=x" * depth
    cols = [expr for _ in range(width)]
    sql = f"SELECT {','.join(cols)}"

    with self.assertRaises(ParseError) as ctx:
        parse_one(sql, max_nodes=10_000)  # the test: 1_000_000
    self.assertIn("Maximum number of AST nodes", str(ctx.exception))

    with self.assertRaises(ParseError) as ctx:
        parse_one(sql, max_nodes=500)  # the test: 50_000
    self.assertIn("(500)", str(ctx.exception))

    with self.assertRaises(ParseError):
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
    self.assertIsInstance(parse_one("SELECT 1, 2, 3"), exp.Select)


# test id -> (stand-in, CPU estimate in ms)
STANDINS = {
    "tests/test_parser.py::TestParser::test_max_nodes": (max_nodes_standin, 400),
}


def install(klass, cls_id, meths, patched_files):
    """Swap in stand-ins for this class's selected tests, unless the test file itself is patched."""
    for meth in meths:
        entry = STANDINS.get(f"{cls_id}::{meth}")
        if entry and cls_id.split("::")[0] not in patched_files:
            setattr(klass, meth, entry[0])
