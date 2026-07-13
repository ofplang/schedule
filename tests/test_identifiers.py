"""Identifier / node-path formatting helpers (core/identifiers.py)."""

from __future__ import annotations

from ofplang.schedule.core.identifiers import format_endpoint, format_node_path


def test_format_node_path_single_level():
    assert format_node_path(("SampleSource",)) == "SampleSource"


def test_format_node_path_nested_uses_slash():
    # Hierarchical paths through nested composites render as a/b/c (SPEC §6.3),
    # never as a Python tuple/list.
    assert format_node_path(("b1", "rep1", "peal")) == "b1/rep1/peal"


def test_format_endpoint_joins_path_and_port():
    assert format_endpoint(("b1", "peal"), "plate") == "b1/peal.plate"
    assert format_endpoint(("SampleSource",), "source_out") == "SampleSource.source_out"
