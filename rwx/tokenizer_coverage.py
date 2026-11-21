"""Build the tokenizer flag read masks from the pristine tree and its coverage.

    python rwx/tokenizer_coverage.py /path/to/pinned/sqlglot
"""

import ast
import dis
import inspect
import json
import sys
import textwrap
from pathlib import Path

from selector import MAP, Map

TOKENS = "sqlglot/tokens.py"
CORE = "sqlglot/tokenizer_core.py"


def mentions(tree, name):
    return [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == name)
        or (isinstance(node, ast.Attribute) and node.attr == name)
        or (isinstance(node, ast.Constant) and node.value == name)
    ]


def build(cmap):
    from sqlglot.tokens import Tokenizer, TokenizerCore

    factory = Tokenizer._init_core
    factory_tree = ast.parse(textwrap.dedent(inspect.getsource(factory)))
    returned = factory_tree.body[0].body[-1]
    if not isinstance(returned, ast.Return):
        return {}
    call = returned.value
    if (
        not isinstance(call, ast.Call)
        or not isinstance(call.func, ast.Name)
        or call.func.id != "TokenizerCore"
    ):
        return {}
    core_class = ast.parse(inspect.getsource(TokenizerCore)).body[0]
    ctor = next(node for node in core_class.body if getattr(node, "name", "") == "__init__")
    declarations = [n for n in core_class.body if not isinstance(n, ast.FunctionDef)]
    slots = next(
        n for n in declarations if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "__slots__"
    )
    methods = {}
    for value in vars(TokenizerCore).values():
        if inspect.isfunction(value):
            code = value.__code__
            methods[f"{code.co_firstlineno}:{code.co_name}"] = code

    factory_ref = (TOKENS, f"{factory.__code__.co_firstlineno}:_init_core", "func")
    ctor_ref = (CORE, f"{TokenizerCore.__init__.__code__.co_firstlineno}:__init__", "func")
    class_ref = (CORE, f"{TokenizerCore.__firstlineno__}:TokenizerCore", "class")
    flags = {}
    for kw in call.keywords:
        attr, slot = kw.value, kw.arg
        if not (
            slot
            and isinstance(attr, ast.Attribute)
            and ast.unparse(attr.value) == "self"
            and type(getattr(Tokenizer, attr.attr, None)) is bool
        ):
            continue
        if len(mentions(factory_tree, attr.attr)) != 1 or slot not in TokenizerCore.__slots__:
            continue
        # Only erase the two copies when there is no other use in either step.
        stores = [
            n
            for n in ctor.body
            if isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and ast.unparse(n.targets[0]) == "self." + slot
            and ast.unparse(n.value) == slot
        ]
        if len(stores) != 1 or len(mentions(ctor, slot)) != 2:
            continue
        if any(mentions(n, slot) for n in declarations if n is not slots):
            continue
        setting_refs = set(cmap.name_refs.get(attr.attr, ()))
        slot_refs = set(cmap.name_refs.get(slot, ()))
        if factory_ref not in setting_refs or not {class_ref, ctor_ref} <= slot_refs:
            continue
        direct = setting_refs - {factory_ref}
        consumers = slot_refs - {class_ref, ctor_ref}
        if not consumers or any(kind != "func" for _, _, kind in direct | consumers):
            continue
        mask, _ = cmap.ref_mask(direct)
        for ref in consumers:
            rel, key, kind = ref
            code = methods.get(key) if rel == CORE and kind == "func" else None
            uses = [i for i in dis.get_instructions(code) if i.argval == slot] if code else []
            lines = cmap.code(rel, key)
            if uses and all(i.opname == "LOAD_ATTR" and i.line_number in lines for i in uses):
                for i in uses:
                    mask |= cmap.masks[lines[i.line_number]]
            else:
                broad, _ = cmap.ref_mask([ref])
                mask |= broad
        if mask:
            flags[attr.attr] = hex(mask)
    base = f"{TOKENS}:{Tokenizer.__firstlineno__}:Tokenizer"
    return {"classes": sorted(cmap.class_desc[base]), "flags": flags}


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    (root / ".rwx-tokenizer.json").write_text(json.dumps(build(Map(MAP)), sort_keys=True))
