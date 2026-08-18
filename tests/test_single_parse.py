"""Each input file is parsed exactly once, and the tree can produce the value.

The scheduling pipeline needs two views of a document: the wrapped tree (positions,
every duplicate entry) for the schema validators, and the ordinary Python value for
the model it builds. It used to get the second by reading the file again -- the
environment twice, the execution document three times. These tests pin the single
parse (a counting regression test, since nothing else would notice a re-read) and
`to_plain`'s fidelity to `yaml.safe_load`, which is what makes it possible.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import yaml

from ofplang.schedule import schedule, validate_document, validate_environment
from ofplang.schedule.core import yamlnode
from ofplang.schedule.validation.document import validate_document_node
from ofplang.schedule.validation.environment import validate_environment_node

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
WORKFLOW = EXAMPLES / "simple.workflow.yaml"
ENV = EXAMPLES / "simple.env.yaml"
STATUS = EXAMPLES / "simple.status.yaml"


# --- to_plain: the value the tree stands for ----------------------------------


def test_to_plain_matches_safe_load():
    """Including the cases where a naive re-resolve would differ: a quoted number
    stays a string, and a date resolves to `datetime.date`, because the tree keeps
    what PyYAML's own constructor made of each scalar."""
    text = (
        "name: simple\n"
        'quoted: "123"\n'
        "count: 7\n"
        "ratio: 1.5\n"
        "flag: true\n"
        "nothing: null\n"
        "when: 2026-08-18\n"
        "nested:\n"
        "  - {a: 1, b: [x, y]}\n"
        "  - []\n"
    )
    plain = yamlnode.to_plain(yamlnode.loads(text, "<text>"))
    assert plain == yaml.safe_load(text)
    assert plain["quoted"] == "123"
    assert plain["when"] == datetime.date(2026, 8, 18)


def test_to_plain_round_trips_an_in_memory_document():
    data = {
        "time": {"unit": "second"},
        "now": 3,
        "activities": [{"kind": "processing", "node": ["S1"], "start": 0.5, "ok": False}],
        "empty": None,
    }
    assert yamlnode.to_plain(yamlnode.from_object(data)) == data


def test_to_plain_is_last_wins_while_keyed_node_access_is_first_wins():
    """The documented asymmetry: `safe_load` (and so `to_plain`) keeps the last
    duplicate entry, while `YMap.get` -- what the validators read -- keeps the first.
    Pinned here so a change to either one is deliberate."""
    text = "a: 1\na: 2\n"
    root = yamlnode.loads(text, "<text>")
    assert yamlnode.to_plain(root) == yaml.safe_load(text) == {"a": 2}
    assert root.get("a").value == 1


def test_to_plain_of_an_empty_document_is_none():
    assert yamlnode.to_plain(yamlnode.loads("", "<text>")) is None


# --- the node-taking validator entries ----------------------------------------


def test_node_entries_agree_with_the_path_entries():
    for path, from_path, from_node in (
        (ENV, validate_environment, validate_environment_node),
        (STATUS, validate_document, validate_document_node),
    ):
        by_path = from_path(path)
        by_node = from_node(yamlnode.load_source(path))
        assert by_path.ok and by_node.ok
        assert [d.code for d in by_node.diagnostics] == [d.code for d in by_path.diagnostics]


def test_node_entries_report_an_empty_document():
    assert not validate_environment_node(None).ok
    assert not validate_document_node(None).ok


# --- the single parse ---------------------------------------------------------


def _count_parses(monkeypatch):
    """Count every YAML parse, by file (a `loads` of wrapped nodes) and in total
    (including `yaml.safe_load`, which the workflow parser uses)."""
    per_file: dict[str, int] = {}
    total = {"n": 0}

    real_loads = yamlnode.loads
    real_safe_load = yaml.safe_load

    def counting_loads(text, file=None):
        name = Path(file).name if file else "<text>"
        per_file[name] = per_file.get(name, 0) + 1
        total["n"] += 1
        return real_loads(text, file)

    def counting_safe_load(stream):
        total["n"] += 1
        return real_safe_load(stream)

    monkeypatch.setattr(yamlnode, "loads", counting_loads)
    monkeypatch.setattr(yaml, "safe_load", counting_safe_load)
    return per_file, total


def test_each_input_file_is_parsed_once(monkeypatch):
    per_file, total = _count_parses(monkeypatch)
    report = schedule(WORKFLOW, ENV, document_path=STATUS, random_seed=0)
    assert report.ok
    # The environment and the document, once each (they used to be 2 and 3), plus the
    # workflow's own `safe_load` -- three parses for three files.
    assert per_file == {ENV.name: 1, STATUS.name: 1}
    assert total["n"] == 3


def test_in_memory_inputs_are_not_parsed_at_all(monkeypatch):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    env = yaml.safe_load(ENV.read_text(encoding="utf-8"))
    status = yaml.safe_load(STATUS.read_text(encoding="utf-8"))
    per_file, total = _count_parses(monkeypatch)
    assert schedule(workflow, env, document_path=status, random_seed=0).ok
    assert per_file == {}
    assert total["n"] == 0

