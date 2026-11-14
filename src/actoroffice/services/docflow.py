"""The flows and gates of a standard pack as the office's own stage templates and gate policies, and which pack a
ticket follows.

A pack stage that only names an id the `code` workflow already has (`build`, `pr`, `close`) takes that stage's outputs
and checks, so a task that goes on to code is built, reviewed and merged exactly like any other code ticket.

The standard is layered: office → team → project → ticket (an epic and everything under it). Each layer is a
`DocProfile`: another pack, other rule severities. A later layer overrides an earlier one key by key.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from actoroffice.doccore import StandardPack, load_default, load_pack
from actoroffice.doccore.model import Spawn
from actoroffice.doccore.pack import PACK_FILE, PackError
from actoroffice.domain.stage import GatePolicy, StageDef, StageTemplate, default_templates

log = logging.getLogger(__name__)

RULE_LEVELS = ("error", "warn", "off")

_pack_path: Path | None = None  # the office's own pack (config `doc_pack`); None = the built-in one
_loaded: dict[str, tuple[tuple[Any, ...], StandardPack]] = {}  # pack folder → (file signature when loaded, pack)
_adjusted: dict[tuple[int, tuple[tuple[str, str], ...]], tuple[StandardPack, StandardPack]] = {}  # (base, overrides) → (base, result)
_derived: dict[int, tuple[StandardPack, dict[str, StageTemplate], dict[str, GatePolicy]]] = {}  # id(pack) → what the office makes of it


class DocProfile(BaseModel):
    """What one layer changes of the standard. Everything left out is taken from the layer before."""

    model_config = ConfigDict(extra="forbid")

    pack: str | None = None  # "default", a name under <data_dir>/packs/, or a folder
    rules: dict[str, str] = Field(default_factory=dict)  # rule id → error | warn | off

    def empty(self) -> bool:
        return not self.pack and not self.rules


def configure(path: Path | None) -> StandardPack:
    """Use another standard pack for this office. It is loaded and checked first: a pack that is not valid raises
    PackError and the pack in use stays as it was."""
    global _pack_path
    pack = load_pack(path) if path is not None else load_default()
    _pack_path = path
    _loaded.clear()
    _adjusted.clear()
    _derived.clear()
    _loaded[_key(path)] = (_signature(pack), pack)
    return pack


def _key(path: Path | None) -> str:
    return str(path) if path is not None else ""


def _signature(pack: StandardPack) -> tuple[Any, ...]:
    """Changes when a pack.yaml of the pack (or of one it extends) is edited."""
    out = []
    for root in pack._roots:
        try:
            out.append((str(root), (root / PACK_FILE).stat().st_mtime_ns))
        except OSError:
            out.append((str(root), 0))
    return tuple(out)


def pack_at(path: Path | None) -> StandardPack:
    """The pack of a folder (None = the built-in one), read again when its file changed. An edit that makes the pack
    invalid is not taken: the last good pack stays in use and the problem is logged."""
    key = _key(path)
    hit = _loaded.get(key)
    if hit is not None and hit[0] == _signature(hit[1]):
        return hit[1]
    try:
        pack = load_pack(path) if path is not None else load_default()
    except PackError as exc:
        if hit is None:
            raise
        log.warning("pack %s changed but is not valid, keeping the one in use: %s", path, exc)
        _loaded[key] = (_signature(hit[1]), hit[1])  # do not try again until the file changes once more
        return hit[1]
    _loaded[key] = (_signature(pack), pack)
    return pack


def default_pack() -> StandardPack:
    return pack_at(_pack_path)


def pack_path(name: str | None, data_dir: Path | None = None) -> Path | None:
    """Where a pack named in a profile lives. None = the built-in pack."""
    if not name or name == "default":
        return None
    direct = Path(name).expanduser()
    if direct.is_absolute() or direct.exists() or data_dir is None:
        return direct
    return data_dir / "packs" / name


def with_rules(pack: StandardPack, rules: dict[str, str]) -> StandardPack:
    """The pack with other severities for some rules; `off` takes a rule out."""
    wanted = {k: v for k, v in rules.items() if k in pack.rules and v in RULE_LEVELS}
    if not wanted:
        return pack
    key = (id(pack), tuple(sorted(wanted.items())))
    hit = _adjusted.get(key)
    if hit is not None and hit[0] is pack:
        return hit[1]
    changed = {}
    for rule_id, rule in pack.rules.items():
        level = wanted.get(rule_id)
        if level == "off":
            continue
        changed[rule_id] = rule.model_copy(update={"severity": level}) if level else rule
    out = pack.model_copy(update={"rules": changed})
    out._roots = list(pack._roots)
    _adjusted[key] = (pack, out)
    return out


def resolve(layers: list[DocProfile], data_dir: Path | None = None) -> StandardPack:
    """The pack a stack of layers gives, nearest layer last. Raises PackError when a named pack cannot be loaded."""
    name: str | None = None
    rules: dict[str, str] = {}
    for layer in layers:
        if layer.pack:
            name = layer.pack
        rules.update(layer.rules)
    base = pack_at(pack_path(name, data_dir)) if name else default_pack()
    return with_rules(base, rules)


def check_profile(profile: DocProfile, before: list[DocProfile], data_dir: Path | None = None) -> StandardPack:
    """A layer about to be saved on top of the layers before it. Raises PackError naming every problem; returns the
    pack the layers give together."""
    problems = [f"rules.{rule_id}: mức '{level}' không hợp lệ; chọn một trong: {', '.join(RULE_LEVELS)}" for rule_id, level in profile.rules.items() if level not in RULE_LEVELS]
    name = profile.pack or next((layer.pack for layer in reversed(before) if layer.pack), None)
    try:
        base = pack_at(pack_path(name, data_dir)) if name else default_pack()  # before any override: a rule switched off earlier can be named again
    except PackError as exc:
        raise PackError([f"pack '{name}': {p}" for p in exc.problems]) from None
    problems += [f"rules.{rule_id}: bộ chuẩn '{base.name}' không có luật này (có: {', '.join(sorted(base.rules))})" for rule_id in profile.rules if rule_id not in base.rules]
    if problems:
        raise PackError(problems)
    return resolve([*before, profile], data_dir)


def _derive(pack: StandardPack) -> tuple[StandardPack, dict[str, StageTemplate], dict[str, GatePolicy]]:
    hit = _derived.get(id(pack))
    if hit is not None and hit[0] is pack:
        return hit
    code = {s.id: s for s in default_templates()["code"].stages}
    templates: dict[str, StageTemplate] = {}
    for name, stages in pack.flows.items():
        built = []
        for cfg in stages:
            base = code.get(cfg.id)
            if base is not None and not cfg.outputs and not cfg.checks:
                built.append(base.model_copy(update={"gate": cfg.gate or base.gate, "instructions": cfg.instructions or base.instructions}))
            else:
                built.append(
                    StageDef(id=cfg.id, title=cfg.title, outputs=list(cfg.outputs), checks=list(cfg.checks), gate=cfg.gate,
                             requires_steps=cfg.requires_steps, instructions=cfg.instructions)
                )  # fmt: skip
        templates[name] = StageTemplate(name=name, stages=built)
    gates = {
        name: GatePolicy(roles=list(g.roles), min=g.min, fallback=g.fallback, external=g.external, submit_by=list(g.submit_by),
                         accept_findings=list(g.accept_findings), show=list(g.show))
        for name, g in pack.gates.items()
    }  # fmt: skip
    if len(_derived) > 64:  # packs replaced by edits: nothing refers to them any more
        _derived.clear()
    _derived[id(pack)] = (pack, templates, gates)
    return _derived[id(pack)]


def pack_templates(pack: StandardPack | None = None) -> dict[str, StageTemplate]:
    return _derive(pack or default_pack())[1]


def pack_gates(pack: StandardPack | None = None) -> dict[str, GatePolicy]:
    return _derive(pack or default_pack())[2]


def is_pack_flow(template: str | None, pack: StandardPack | None = None) -> bool:
    return bool(template) and template in (pack or default_pack()).flows


def spawn_of(template: str | None, stage_id: str | None, pack: StandardPack | None = None) -> Spawn | None:
    """What a stage creates when its gate is approved (the child tickets of a breakdown), if anything."""
    for cfg in (pack or default_pack()).flows.get(template or "", []):
        if cfg.id == stage_id:
            return cfg.spawn
    return None


def same_road(pack: StandardPack, a: str, b: str) -> bool:
    """Two flows a ticket can move between without redoing anything: one is the beginning of the other (`task-spec`
    is `task` without the stages after design)."""
    first, second = [s.id for s in pack.flows.get(a, [])], [s.id for s in pack.flows.get(b, [])]
    if not first or not second or a == b:
        return False
    short, long = sorted((first, second), key=len)
    return long[: len(short)] == short
