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
  or one naming nothing in scope (`unknown_reference`), or an output its node has but
  does not expose -- a fold output dropped by the section or by default
  (`output_not_exposed`). One that is not a mapping at
  all is refused earlier, by the reader's shape guard (`wrong_value_kind`), with the
  other shapes it cannot walk;
- a binding the reader does not look at: a section its node kind does not take
  (`section_not_valid_for_kind`), or an entry naming no input port of the target --
  for a branch, of either explicit arm, since its `args` must suit both
  (`binding_port_not_found`);
- a branch condition the reader cannot resolve, and so cannot decide the arm by
  (`unknown_reference`, `malformed_reference`);
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
# `do_while` is not expanded at all (reported as unsupported), so its sections are
# nobody's to check here.
_SECTIONS = {
    None: ("state", "bind"),
    "map": ("each", "bind"),
    "fold": ("each", "carry", "bind"),
    "branch": ("args",),
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
    # What a body reference can name: the composite's inputs, and each node's outputs
    # -- the ones its target has (`declared`), and of those the ones it exposes.
    declared = {
        node.get("id"): _declared_outputs(node, procs)
        for node in nodes
        if isinstance(node.get("id"), str)
    }
    exposed = {
        node.get("id"): _exposed_outputs(node, procs, object_bearing)
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
        known = right in inputs if left == "inputs" else right in declared.get(left, ())
        if not known:
            diags.error(v0.UNKNOWN_REFERENCE, f"unresolved reference {ref!r}", path)
            return None
        visible = exposed.get(left)
        if left != "inputs" and visible is not None and right not in visible:
            diags.error(
                v0.OUTPUT_NOT_EXPOSED,
                f"{ref!r} names an output node {left!r} does not expose (spec 18, 21)",
                path,
            )
            return None
        return "from"

    for i, node in enumerate(nodes):
        kind = node.get("kind")
        if kind not in _SECTIONS:
            continue
        npath = f"{base}.nodes[{i}]"
        # The processes this node's bindings supply: its one target, or each explicit
        # arm of a branch, whose `args` must suit both (v0 20, requirement 2). An
        # implicit `else` takes whatever it is given and returns its Objects as they
        # came, so it adds no ports to check against.
        targets = _targets(node, procs)
        if not targets:
            continue  # an undefined process is reported where the node is expanded
        if kind == "branch":
            # The condition is a body reference the reader resolves to decide the arm.
            condition = node.get("condition")
            if isinstance(condition, dict):
                check_source(condition, f"{npath}.condition")
        allowed = _SECTIONS[kind]
        for section in _ALL_SECTIONS:
            if node.get(section) is not None and section not in allowed:
                diags.error(
                    v0.SECTION_NOT_VALID_FOR_KIND,
                    f"a {kind or 'process'} node takes no {section} section",
                    f"{npath}.{section}",
                )
        # Each binding's source, checked once however many arms it supplies.
        sources: dict[tuple[str, str], str | None] = {}
        for section in allowed:
            entries = node.get(section)
            if not isinstance(entries, dict):
                continue  # absent, or unreadable and reported by the shape pass
            for port, binding in entries.items():
                sources[(section, port)] = check_source(binding, f"{npath}.{section}.{port}")
        literal_reported: set[str] = set()
        for tname, target in targets:
            ports = _mapping(target.get("inputs"))
            bound: set[str] = set()
            for (section, port), source in sources.items():
                path = f"{npath}.{section}.{port}"
                if port not in ports:
                    diags.error(
                        v0.BINDING_PORT_NOT_FOUND,
                        f"{port!r} is not an input port of {tname!r}",
                        path,
                    )
                    continue
                bound.add(port)
                spec = ports.get(port) or {}
                if (
                    source == "value"
                    and object_bearing(str(spec.get("type", "")))
                    and path not in literal_reported
                ):
                    literal_reported.add(path)
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
                        f"input port {port!r} of {tname!r} is not bound",
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


def _targets(node: dict, procs: dict) -> list[tuple[str, dict]]:
    """The processes a node invokes, by name: its `process`, or a branch's explicit
    `then` / `else` arms (an implicit `else` invokes nothing)."""
    if node.get("kind") == "branch":
        names = [
            arm.get("process") for arm in (node.get("then"), node.get("else"))
            if isinstance(arm, dict)
        ]
    else:
        names = [node.get("process")]
    return [
        (name, procs[name])
        for name in names
        if isinstance(name, str) and isinstance(procs.get(name), dict)
    ]


def _branch_object_outputs(node: dict, procs: dict, object_bearing) -> tuple[set, set] | None:
    """A branch's Object-bearing outputs per arm, `(then, else)`: an implicit `else`
    returns each Object-bearing argument as the same-name output (v0 20). None where
    the `then` arm or an explicit `else` names no process."""
    then = node.get("then")
    then_proc = procs.get(then.get("process")) if isinstance(then, dict) else None
    if not isinstance(then_proc, dict):
        return None

    def objects(ports) -> set:
        return {
            port for port, spec in _mapping(ports).items()
            if object_bearing(str(spec.get("type", "")) if isinstance(spec, dict) else "")
        }

    then_obj = objects(then_proc.get("outputs"))
    else_arm = node.get("else")
    if isinstance(else_arm, dict):
        else_proc = procs.get(else_arm.get("process"))
        if not isinstance(else_proc, dict):
            return None
        return then_obj, objects(else_proc.get("outputs"))
    then_inputs = objects(then_proc.get("inputs"))
    return then_obj, {arg for arg in _mapping(node.get("args")) if arg in then_inputs}


def _declared_outputs(node: dict, procs: dict) -> set[str]:
    """The outputs a body node's target has: what a reference can name at all. For a
    branch, what either arm has -- an implicit `else` the Object-bearing arguments it
    returns, which are the `then` arm's same-name inputs (v0 20)."""
    if node.get("kind") == "branch":
        names: set[str] = set()
        for _name, proc in _targets(node, procs):
            names |= set(_mapping(proc.get("outputs")))
        return names
    target = procs.get(node.get("process"))
    outputs = target.get("outputs") if isinstance(target, dict) else None
    return set(outputs) if isinstance(outputs, dict) else set()


def _exposed_outputs(
    node: dict, procs: dict, object_bearing: Callable[[str], bool]
) -> set[str] | None:
    """The outputs a body node makes visible to its siblings, or None for all it has.

    An ordinary node and a `map` expose everything (None). A `fold` exposes what its
    `outputs` section lists as `carry` / `collect` -- or, without one, its carried
    ports (v0 18.1, 18.2). None too where the fold's section is one the reader
    refuses (`_Expander._fold_modes`): what it exposes is then the thing refused, and
    a reference to one of its outputs is that refusal's consequence, not a second
    mistake -- the rule ofplang-validate follows.

    A `branch` exposes what its `outputs` lists as `common` -- without one, the
    Object-bearing outputs common to both arms (v0 20.1, 20.3). None where its own
    output rules are broken: an entry with no valid mode, an Object-bearing output not
    listed as common, a one-sided Object output."""
    if node.get("kind") == "branch":
        arms = _branch_object_outputs(node, procs, object_bearing)
        if arms is None or arms[0] != arms[1]:
            return None
        section = node.get("outputs")
        if not isinstance(section, dict):
            return set(arms[0])
        modes = {
            port: spec.get("mode") if isinstance(spec, dict) else None
            for port, spec in section.items()
        }
        if any(mode not in ("common", "drop") for mode in modes.values()):
            return None
        if any(modes.get(port) != "common" for port in arms[0]):
            return None
        return {port for port, mode in modes.items() if mode == "common"}
    if node.get("kind") != "fold":
        return None
    target = procs.get(node.get("process"))
    outputs = target.get("outputs") if isinstance(target, dict) else None
    if not isinstance(outputs, dict):
        return None
    carry_section = node.get("carry")
    carry = set(carry_section) if isinstance(carry_section, dict) else set()
    objects = {
        port for port, spec in outputs.items()
        if object_bearing(str((spec or {}).get("type", "")) if isinstance(spec, dict) else "")
    }
    section = node.get("outputs")
    if not isinstance(section, dict):
        if objects - carry:
            return None  # refused: a non-carry Object output needs a section (18.2)
        return set(outputs) & carry
    modes = {
        port: spec.get("mode") if isinstance(spec, dict) else None
        for port, spec in section.items()
    }
    if any(mode not in ("carry", "collect", "drop") for mode in modes.values()):
        return None  # an entry with no mode, or an invalid one
    if set(modes) != set(outputs) or any(modes[port] == "drop" for port in objects):
        return None  # a listing that is not complete, or an Object dropped
    return {port for port, mode in modes.items() if mode in ("carry", "collect")}
