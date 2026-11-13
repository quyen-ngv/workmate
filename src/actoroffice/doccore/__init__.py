"""Doc Core: what makes a set of documents "standard and matching", with no I/O.

Pure: only the standard library, pydantic and yaml. Nothing here imports the rest of actoroffice
(tests/test_doccore.py). The office reaches the outside world through adapters and hands the results in as data.
"""

from .model import Approved, Baseline, DocType, Evidence, Finding, Item, ItemKind, MatchState, ParsedDoc, Problem, RuleDef, SourceRef
from .pack import PackError, StandardPack, build_pack, load_default, load_pack
from .parse import parse, parse_sources
from .rules import PRIMITIVES, TRACE_VALID, match_state, run_rules
from .trace import TraceGraph, impact

__all__ = [
    "PRIMITIVES",
    "TRACE_VALID",
    "Approved",
    "Baseline",
    "DocType",
    "Evidence",
    "Finding",
    "Item",
    "ItemKind",
    "MatchState",
    "PackError",
    "ParsedDoc",
    "Problem",
    "RuleDef",
    "SourceRef",
    "StandardPack",
    "TraceGraph",
    "build_pack",
    "impact",
    "load_default",
    "load_pack",
    "match_state",
    "parse",
    "parse_sources",
    "run_rules",
]
