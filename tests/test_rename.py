from __future__ import annotations

import pytest

from actoroffice.domain.errors import Conflict, OfficeError
from actoroffice.domain.knowledge import SkillRef, SkillScope

from .test_telegram import hire, office  # noqa: F401  (fixture)


async def test_suggested_names_are_english_and_unused(office):
    assert await office.org.suggest_name() == "Emma"
    await hire(office, "ba", "Emma")
    assert await office.org.suggest_name() == "Liam"


async def test_rename_moves_every_reference_to_the_new_name(office):
    hoa = await hire(office, "ba", "Hoa")
    await office.skills.save(SkillRef(scope=SkillScope.ACTOR, owner="Hoa", name="phong-van"), "Phỏng vấn", "Hỏi 5 câu", author="Hoa")
    ticket = await office.tickets.create("Nghỉ phép", assignee_actor_id=hoa.id)
    await office.tickets.add_comment(ticket.id, "Em nhận việc", author_kind="actor", author_id="Hoa")
    dm = await office.chat.dm(hoa.id)
    auto = await office.automations.save(
        "giao-bao-cao", steps=[{"type": "agent", "actor": "Hoa", "title": "Báo cáo tuần", "prompt": "Làm báo cáo"}],
        description="Giao việc hằng tuần", owner_actor_id=hoa.id, conversation_id=dm.id, schedule="weekly:mon@08:00",
    )
    await office.automations.approve(auto.id, office.automations.fingerprint(auto.name, auto.steps))

    emma = await office.rename_actor(hoa.id, "Emma")

    assert emma.name == "Emma" and (await office.org.find_actor("emma")).id == hoa.id and await office.org.find_actor("Hoa") is None
    assert [s.ref.name for s in office.skills.list(SkillScope.ACTOR, "Emma")] == ["phong-van"]
    assert not office.skills.list(SkillScope.ACTOR, "Hoa")
    assert (await office.tickets.comments(ticket.id))[-1].author_id == "Emma"
    assert (await office.chat.get(dm.id)).title == "Emma"
    renamed = await office.automations.get(auto.id)
    assert renamed.steps[0].actor == "Emma"
    assert renamed.approved_sha == office.automations.fingerprint(renamed.name, renamed.steps)  # still approved


async def test_rename_rejects_taken_and_invalid_names(office):
    hoa = await hire(office, "ba", "Hoa")
    await hire(office, "dev", "Minh")
    with pytest.raises(Conflict):
        await office.rename_actor(hoa.id, "minh")
    with pytest.raises(OfficeError):
        await office.rename_actor(hoa.id, "Hai Hoa")
    assert (await office.rename_actor(hoa.id, "hoa")).name == "hoa"  # only the letter case changes
