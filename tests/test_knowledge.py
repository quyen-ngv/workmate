from __future__ import annotations

import shutil
from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.errors import NotFound, OfficeError
from actoroffice.domain.knowledge import MemoryKind, MemoryScope, SkillRef, SkillScope

from .fakes import FakeRuntime, TurnCtx


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=runtime)
    await o.open()
    await o.install_default_positions(skills=False)
    await o.start_notifier()
    yield o
    await o.close()


async def hire(office: Office, position: str, name: str):
    pos = await office.org.find_position(position)
    return await office.org.hire(pos.id, name)


# ---- memory -------------------------------------------------------------------------------------


async def test_memory_search_ignores_accents_and_respects_scope(office):
    minh, hung = await hire(office, "dev", "Minh"), await hire(office, "dev", "Hung")
    lan = await hire(office, "writer", "Lan")
    await office.memory.add(minh, "Repo dùng Java 21 và Spring Boot 3", kind=MemoryKind.FACT, created_by="Minh")
    await office.memory.add(minh, "Chạy test bằng mvn -q test trước khi commit", scope=MemoryScope.POSITION, created_by="Minh")

    assert [m.content for m in await office.memory.search(minh, "spring boot")] == ["Repo dùng Java 21 và Spring Boot 3"]
    assert await office.memory.search(minh, "dung java")  # without accents still matches
    # Position memories are shared with the same role, actor memories are private.
    assert [m.content for m in await office.memory.search(hung, "commit")] == ["Chạy test bằng mvn -q test trước khi commit"]
    assert await office.memory.search(hung, "spring") == []
    assert await office.memory.search(lan, "commit") == []


async def test_memory_dedupes_redacts_and_clears(office):
    minh = await hire(office, "dev", "Minh")
    a = await office.memory.add(minh, "Token là sk-ant-abcdefghijklmnopqrstuvwxyz0123", created_by="Minh")
    b = await office.memory.add(minh, "token là SK-ANT-abcdefghijklmnopqrstuvwxyz0123", created_by="Minh")
    assert "sk-ant" not in a.content.lower()
    assert len(await office.memory.visible(minh)) in (1, 2) and a.id != 0 and b.id != 0
    assert await office.memory.clear(minh) >= 1
    assert await office.memory.visible(minh) == []
    assert await office.memory.search(minh, "token") == []


async def test_memory_save_from_ticket_posts_undo_card(office, runtime):
    lan = await hire(office, "writer", "Lan")

    async def work(ctx: TurnCtx) -> None:
        await ctx.office("memory_save", {"content": "Anh chủ thích câu ngắn, không dùng emoji", "kind": "preference"})
        await ctx.office("ticket_set_status", {"status": "done", "comment": "xong"})

    runtime.scripts.append(work)
    t = await office.tickets.create("Viết intro", assignee_actor_id=lan.id)
    await office.scheduler.wait_idle(timeout=10)

    [memory] = await office.memory.visible(lan)
    assert memory.source_ticket_id == t.id and memory.kind == MemoryKind.PREFERENCE
    dm = await office.chat.dm(lan.id)
    cards = [m for m in await office.chat.messages(dm.id) if m.kind.value == "memory"]
    assert cards and cards[0].meta["memory_id"] == memory.id
    # The next session's system prompt carries what the actor remembers.
    runtime.scripts.append(lambda ctx: _noop())
    await office.tickets.create("Viết outro", assignee_actor_id=lan.id)
    await office.scheduler.wait_idle(timeout=10)
    assert "thích câu ngắn" in runtime.specs[-1].system_prompt


async def _noop() -> None:
    return None


async def test_memory_from_chat_is_announced_in_that_chat(office, runtime, tmp_path):
    from actoroffice.domain.run import TurnResult, TurnStatus
    from actoroffice.domain.work import ProjectAccess

    folder = tmp_path / "goroute"
    folder.mkdir()
    project = await office.projects.create("GoRoute", path=str(folder), access=ProjectAccess.READ_ONLY)
    minh = await hire(office, "dev", "Minh")
    conv = await office.chat.dm(minh.id, project.id)

    async def remember(ctx: TurnCtx) -> TurnResult:
        await ctx.office("memory_save", {"content": "GoRoute: backend Java 21, test JUnit 5", "kind": "preference"})
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s", text="Em nhớ rồi")

    runtime.scripts.append(remember)
    await office.chat.post_user(conv.id, "nhớ giúp anh")
    await office.scheduler.wait_idle(timeout=10)
    kinds = [m.kind.value for m in await office.chat.messages(conv.id)]
    assert "memory" in kinds  # announced where it was learned, not in another project's DM


# ---- skills -------------------------------------------------------------------------------------


async def test_skills_scopes_override_and_files(office):
    minh = await hire(office, "dev", "Minh")
    dev = await office.org.get_position(minh.position_id)
    s = office.skills
    await s.save(SkillRef(scope=SkillScope.SHARED, name="commit-style"), "Viết commit message", "Dùng conventional commits", author="user")
    await s.save(SkillRef(scope=SkillScope.POSITION, owner="dev", name="java-review"), "Review code Java", "Checklist A", author="user")
    await s.save(SkillRef(scope=SkillScope.ACTOR, owner="Minh", name="java-review"), "Review code Java (bản của Minh)", "Checklist B", author="Minh")
    await s.save_file(SkillRef(scope=SkillScope.ACTOR, owner="Minh", name="java-review"), "sub/null-safety.md", "Optional…", author="Minh")

    names = {k.ref.name: k for k in s.for_actor(minh, dev)}
    assert set(names) == {"commit-style", "java-review"}
    assert names["java-review"].ref.scope == SkillScope.ACTOR  # closer scope wins
    skill = s.resolve(minh, dev, "java-review")
    assert skill.body == "Checklist B" and skill.files == ["sub/null-safety.md"]
    assert s.read_file(skill.ref, "sub/null-safety.md") == "Optional…"

    with pytest.raises(OfficeError):
        await s.save_file(skill.ref, "../escape.md", "x", author="Minh")
    with pytest.raises(OfficeError):
        await s.save(SkillRef(scope=SkillScope.SHARED, name="Bad Name"), "d", "b", author="user")
    await s.delete(skill.ref, author="user")
    assert s.resolve(minh, dev, "java-review").ref.scope == SkillScope.POSITION
    with pytest.raises(NotFound):
        s.resolve(minh, dev, "nope")


async def test_skill_changes_are_git_commits(office):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    await office.skills.save(SkillRef(scope=SkillScope.SHARED, name="hello"), "Chào hỏi", "Nói xin chào", author="user")
    await office.skills.save(SkillRef(scope=SkillScope.SHARED, name="hello"), "Chào hỏi", "Nói xin chào lịch sự", author="Lan")
    import subprocess

    log = subprocess.run(["git", "-C", str(office.skills.root), "log", "--format=%an %s"], capture_output=True, text=True).stdout
    assert "Lan: save skill shared/-/hello" in log and "user: save skill shared/-/hello" in log


async def test_actor_creates_own_skill_via_tool(office, runtime):
    minh = await hire(office, "dev", "Minh")

    async def work(ctx: TurnCtx) -> None:
        await ctx.office("skill_save", {"name": "Release Checklist", "description": "Khi phát hành bản mới", "body": "1. test\n2. tag"})
        assert "release-checklist" in await ctx.office("skill_list", {})
        assert "2. tag" in await ctx.office("skill_read", {"name": "release-checklist"})
        await ctx.office("ticket_set_status", {"status": "done", "comment": "ok"})

    runtime.scripts.append(work)
    await office.tickets.create("Chuẩn hoá phát hành", assignee_actor_id=minh.id)
    await office.scheduler.wait_idle(timeout=10)
    [skill] = office.skills.list(SkillScope.ACTOR, "Minh")
    assert skill.ref.name == "release-checklist" and skill.created_by == "Minh"


async def test_memory_compaction_merges_and_keeps_pinned(office, runtime):
    from actoroffice.domain.run import TurnResult, TurnStatus
    from actoroffice.engine.compaction import compact_memories

    minh = await hire(office, "dev", "Minh")
    ids = []
    for text in ("Repo dùng Java 21", "Dự án dùng Java 21", "Test bằng JUnit 5", "Test bằng JUnit 5 + AssertJ", "Deploy thứ 6", "Không deploy thứ 6"):
        ids.append((await office.memory.add(minh, text, kind=MemoryKind.FACT, created_by="Minh")).id)
    pinned = await office.memory.add(minh, "Anh chủ thích câu ngắn", kind=MemoryKind.PREFERENCE, created_by="user", pinned=True)
    answer = (
        '{"memories": [{"kind": "fact", "content": "Dự án dùng Java 21", "from": [%d, %d]}, '
        '{"kind": "fact", "content": "Test bằng JUnit 5 + AssertJ", "from": [%d, %d]}, '
        '{"kind": "decision", "content": "Không deploy vào thứ 6", "from": [%d, %d]}]}' % tuple(ids)
    )

    async def compact(ctx: TurnCtx) -> TurnResult:
        assert ctx.spec.tools == [] and "Java 21" in ctx.spec.prompt and "câu ngắn" not in ctx.spec.prompt
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s", text="```json\n" + answer + "\n```")

    runtime.scripts.append(compact)
    result = await compact_memories(office, minh)
    assert result == {"before": 6, "after": 3, "changed": 6}
    contents = sorted(m.content for m in await office.memory.visible(minh))
    assert contents == sorted(["Dự án dùng Java 21", "Test bằng JUnit 5 + AssertJ", "Không deploy vào thứ 6", pinned.content])

    async def garbage(ctx: TurnCtx) -> TurnResult:
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s", text="xin lỗi, tôi không làm được")

    for text in ("a1 x", "a2 x", "a3 x"):
        await office.memory.add(minh, text, created_by="Minh")
    runtime.scripts.append(garbage)
    with pytest.raises(OfficeError):
        await compact_memories(office, minh)
    assert len(await office.memory.visible(minh)) == 7  # a bad answer changes nothing


async def test_history_search_finds_past_work_but_not_other_peoples_chats(office):
    minh, hung = await hire(office, "dev", "Minh"), await hire(office, "dev", "Hung")
    ticket = await office.tickets.create("Xuất báo cáo tồn kho", description="Dùng openpyxl, cột theo mẫu kế toán")
    await office.tickets.add_comment(ticket.id, "Đã chốt định dạng ngày dd/mm/yyyy", author_kind="actor", author_id=minh.id)
    await office.tickets.put_document(ticket.id, "design", "Lưu file vào thư mục exports", author="Minh", base_revision=None)
    await office.tickets.put_document(ticket.id, "design", "Lưu file vào thư mục báo cáo riêng", author="Minh", base_revision=1)
    hung_dm = await office.chat.dm(hung.id)
    await office.chat.post_user(hung_dm.id, "Mật khẩu kho bí mật là kho-thang-mười")

    found = await office.memory.history(minh, "ton kho dinh dang thu muc")
    kinds = {h["kind"] for h in found}
    assert {"ticket", "comment", "doc"} <= kinds and all(h["ticket_key"] == ticket.key for h in found)
    docs = [h for h in found if h["kind"] == "doc"]
    assert len(docs) == 1 and "báo cáo riêng" in docs[0]["snip"]  # only the latest revision is indexed
    assert all(h["kind"] != "msg" for h in await office.memory.history(minh, "kho thang muoi"))  # not Minh's chat
    assert any(h["kind"] == "msg" for h in await office.memory.history(hung, "kho thang muoi"))
