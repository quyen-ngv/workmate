"""The flows and gates of the standard pack as the office's own stage templates and gate policies.

A pack stage that only names an id the `code` workflow already has (`build`, `pr`, `close`) takes that stage's outputs
and checks, so a task that goes on to code is built, reviewed and merged exactly like any other code ticket.
"""

from __future__ import annotations

from functools import cache

from actoroffice.doccore import StandardPack, load_default
from actoroffice.doccore.model import Spawn
from actoroffice.domain.stage import GatePolicy, StageDef, StageTemplate, default_templates


@cache
def default_pack() -> StandardPack:
    return load_default()


@cache
def pack_templates() -> dict[str, StageTemplate]:
    code = {s.id: s for s in default_templates()["code"].stages}
    out: dict[str, StageTemplate] = {}
    for name, stages in default_pack().flows.items():
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
        out[name] = StageTemplate(name=name, stages=built)
    return out


@cache
def pack_gates() -> dict[str, GatePolicy]:
    return {
        name: GatePolicy(roles=list(g.roles), min=g.min, fallback=g.fallback, external=g.external, submit_by=list(g.submit_by),
                         accept_findings=list(g.accept_findings), show=list(g.show))
        for name, g in default_pack().gates.items()
    }  # fmt: skip


def is_pack_flow(template: str | None) -> bool:
    return bool(template) and template in default_pack().flows


def spawn_of(template: str | None, stage_id: str | None) -> Spawn | None:
    """What a stage creates when its gate is approved (the child tickets of a breakdown), if anything."""
    for cfg in default_pack().flows.get(template or "", []):
        if cfg.id == stage_id:
            return cfg.spawn
    return None
