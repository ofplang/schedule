"""What the workflow reader refuses rather than silently leave out (design.md D60 Q2).

The reader assumes valid v0 -- validating is `ofplang-validate`'s job, run once at each
CLI's front door -- but it cannot assume it was *given* valid v0: `--no-validate`, and
a caller of the API, hand it whatever they have. Where its own reading would then drop
something without a word, it stops instead. This module is that list, and only that:
not a second validator (design.md D60 V1). Each check sits where the reader would lose
information, and answers with the code `ofplang-validate` gives the same document, so
the two never describe one mistake in two ways.

What the reader would lose, and so what is checked, per composite body:

- a binding it cannot read: neither or both of `from` / `value`
  (`binding_source_arity`), a `from` that is not a reference (`malformed_reference`),
  or one naming nothing in scope (`unknown_reference`). One that is not a mapping at
  all is refused earlier, by the reader's shape guard (`wrong_value_kind`), with the
  other shapes it cannot walk;
- a binding the reader does not look at: a section its node kind does not take
  (`section_not_valid_for_kind`), or an entry naming no input port of the target
  (`binding_port_not_found`);
- a literal bound to an Object-bearing port, which names no Object to move
  (`literal_on_object_port`);
- an input port nothing binds, which has no value to read (`data_indegree` for Pure
  Data, `object_input_no_source` for an Object-bearing one);
- a `map` / `fold` with no `each` source (`missing_each_source`);
- the body's `returns` against the composite's outputs, one to one (spec 12.3,
  revision 0.5): an output with no entry (`output_not_returned`), an entry naming no
  output (`return_port_not_found`).

Everything here is decided by the document alone, so each body is checked once,
whatever invokes it and however often.
"""

from __future__ import annotations

from collections.abc import Callable

from ofplang.validate import errors as v0

from ofplang.schedule.core.diagnostics import Diagnostics

# The binding sections each node kind takes, and so the ones the reader reads (v0 21.0).
# `do_while` and `branch` are not expanded at all (reported as unsupported), so their
# sections are nobody's to check here.
_SECTIONS = {
    None: ("state", "bind"),
    "map": ("each", "bind"),
    "fold": ("each", "carry", "bind"),
}
_ALL_SECTIONS = ("state", "bind", "each", "carry", "args")


def check_body(
    pname: str,
    proc: dict,
    procs: dict,
    object_bearing: Callable[[str], bool],
    diags: Diagnostics,
) -> None:
    """Check one composite's body for what the reader would otherwise drop.

    `object_bearing(type_expr)` says whether a declared type carries an Object slot."""
    body = proc.get("body")
    if not isinstance(body, dict):
        return  # an unreadable body is reported by the shape pass
    base = f"processes.{pname}.body"
    nodes = [n for n in body.get("nodes") or [] if isinstance(n, dict)]
    inputs = _mapping(proc.get("inputs"))
    # What a body reference can name: the composite's inputs, and each node's outputs.
    exposed = {
        node.get("id"): _exposed_outputs(node, procs)
        for node in nodes
        if isinstance(node.get("id"), str)
    }

    def check_source(binding, path: str) -> str | None:
        """Check one binding / return entry; its kind ("from" / "value"), or None."""
        if not isinstance(binding, dict):
            return None  # the reader's shape guard has refused it (`wrong_value_kind`)
        has_from, has_value = "from" in binding, "value" in binding
        if has_from == has_value:
            diags.error(
                v0.BINDING_SOURCE_ARITY,
                "a binding has exactly one of from and value",
                path,
            )
            return None
        if has_value:
            return "value"
        ref = binding.get("from")
        if not isinstance(ref, str) or ref.count(".") < 1 or not all(ref.split(".", 1)):
            diags.error(v0.MALFORMED_REFERENCE, f"malformed reference {ref!r}", path)
            return None
        left, right = ref.split(".", 1)
        known = right in inputs if left == "inputs" else right in exposed.get(left, ())
        if not known:
            diags.error(v0.UNKNOWN_REFERENCE, f"unresolved reference {ref!r}", path)
            return None
        return "from"

    for i, node in enumerate(nodes):
        kind = node.get("kind")
        if kind not in _SECTIONS:
            continue
        target = procs.get(node.get("process"))
        if not isinstance(target, dict):
            continue  # an undefined process is reported where the node is expanded
        npath = f"{base}.nodes[{i}]"
        ports = _mapping(target.get("inputs"))
        allowed = _SECTIONS[kind]
        bound: set[str] = set()
        for section in _ALL_SECTIONS:
            entries = node.get(section)
            if entries is None:
                continue
            if section not in allowed:
                diags.error(
                    v0.SECTION_NOT_VALID_FOR_KIND,
                    f"a {kind or 'process'} node takes no {section} section",
                    f"{npath}.{section}",
                )
                continue
            if not isinstance(entries, dict):
                continue  # an unreadable section is reported by the shape pass
            for port, binding in entries.items():
                path = f"{npath}.{section}.{port}"
                if port not in ports:
                    diags.error(
                        v0.BINDING_PORT_NOT_FOUND,
                        f"{port!r} is not an input port of {node.get('process')!r}",
                        path,
                    )
                    continue
                bound.add(port)
                source = check_source(binding, path)
                spec = ports.get(port) or {}
                if source == "value" and object_bearing(str(spec.get("type", ""))):
                    diags.error(
                        v0.LITERAL_ON_OBJECT_PORT,
                        f"a literal is bound to Object-bearing port {port!r}",
                        path,
                    )
        for port, spec in ports.items():
            if port not in bound:
                carries = object_bearing(str((spec or {}).get("type", "")))
                diags.error(
                    v0.OBJECT_INPUT_NO_SOURCE if carries else v0.DATA_INDEGREE,
                    f"input port {port!r} of {node.get('process')!r} is not bound",
                    npath,
                )
        if kind in ("map", "fold") and not node.get("each"):
            diags.error(
                v0.MISSING_EACH_SOURCE,
                f"{kind} node {node.get('id')!r} has no each source",
                npath,
            )

    # The returns and the composite's outputs, one to one (spec 12.3, revision 0.5).
    returns = _mapping(body.get("returns"))
    outputs = _mapping(proc.get("outputs"))
    for name, entry in returns.items():
        path = f"{base}.returns.{name}"
        if name not in outputs:
            diags.error(
                v0.RETURN_PORT_NOT_FOUND,
                f"returns entry {name!r} names no output port of {pname!r}",
                path,
            )
            continue
        check_source(entry, path)
    for name in outputs:
        if name not in returns:
            diags.error(
                v0.OUTPUT_NOT_RETURNED,
                f"output port {name!r} of {pname!r} has no returns entry",
                f"{base}.returns",
            )


def _mapping(value) -> dict:
    """`value` where it is a mapping, else an empty one: a section the shape guard has
    already reported is nothing to check here."""
    return value if isinstance(value, dict) else {}


def _exposed_outputs(node: dict, procs: dict) -> set[str]:
    """The outputs a body node makes visible to its siblings: its target's, for an
    ordinary node or a `map`; for a `fold`, those its `outputs` section exposes as
    `carry` / `collect` -- or, without one, its carried ports (v0 18.1, 18.2)."""
    target = procs.get(node.get("process"))
    declared = set((target or {}).get("outputs") or {}) if isinstance(target, dict) else set()
    if node.get("kind") != "fold":
        return declared
    section = node.get("outputs")
    if isinstance(section, dict):
        return {
            port for port, spec in section.items()
            if isinstance(spec, dict) and spec.get("mode") in ("carry", "collect")
        }
    carry = node.get("carry")
    return declared & set(carry) if isinstance(carry, dict) else set()
