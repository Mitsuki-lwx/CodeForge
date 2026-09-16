"""共享任务 Store 单元测试。

覆盖：create/get/list_、is_ready 计算、add_blocked_by/add_blocks 双向维护、
status 过滤、update 持久化。
"""

from __future__ import annotations

from core.team.tasks import Filter, Patch, Status, Store, Task


async def test_create_id_format(tmp_path):
    store = Store(str(tmp_path / "tasks.json"))
    tid = await store.create(Task(id="", title="写 README"))
    assert tid.startswith("task_")
    assert len(tid) == len("task_") + 6  # task_ + 6 位 hex


async def test_create_get_round_trip(tmp_path):
    store = Store(str(tmp_path / "tasks.json"))
    tid = await store.create(Task(id="", title="写 README", status=Status.PENDING))
    got = await store.get(tid)
    assert got is not None
    assert got.title == "写 README"
    assert got.status is Status.PENDING
    assert got.created_at > 0


async def test_status_filter(tmp_path):
    store = Store(str(tmp_path / "tasks.json"))
    a = await store.create(Task(id="", title="t1", status=Status.PENDING))
    b = await store.create(Task(id="", title="t2", status=Status.COMPLETED))
    pending = await store.list_(Filter(status=Status.PENDING))
    assert [t.id for t in pending] == [a]
    all_tasks = await store.list_()
    assert {t.id for t in all_tasks} == {a, b}


async def test_update_title_and_status(tmp_path):
    store = Store(str(tmp_path / "tasks.json"))
    tid = await store.create(Task(id="", title="old", status=Status.PENDING))
    updated = await store.update(tid, Patch(title="new", status=Status.IN_PROGRESS))
    assert updated.title == "new"
    assert updated.status is Status.IN_PROGRESS
    got = await store.get(tid)
    assert got.title == "new"


async def test_add_blocked_by_bidirectional(tmp_path):
    """A.add_blocked_by=[B] ⟹ A.blocked_by ∋ B 且 B.blocks ∋ A。"""
    store = Store(str(tmp_path / "tasks.json"))
    b_id = await store.create(Task(id="", title="前置 B"))
    a_id = await store.create(Task(id="", title="A 依赖 B"))
    await store.update(a_id, Patch(add_blocked_by=[b_id]))
    a = await store.get(a_id)
    b = await store.get(b_id)
    assert b_id in a.blocked_by
    assert a_id in b.blocks


async def test_add_blocks_bidirectional(tmp_path):
    """A.add_blocks=[B] ⟹ A.blocks ∋ B 且 B.blocked_by ∋ A。"""
    store = Store(str(tmp_path / "tasks.json"))
    b_id = await store.create(Task(id="", title="B"))
    a_id = await store.create(Task(id="", title="A 后置 B"))
    await store.update(a_id, Patch(add_blocks=[b_id]))
    a = await store.get(a_id)
    b = await store.get(b_id)
    assert b_id in a.blocks
    assert a_id in b.blocked_by


async def test_is_ready_reflects_blockers(tmp_path):
    """含未完成 blocker 的任务 not ready；全部完成才 ready。"""
    store = Store(str(tmp_path / "tasks.json"))
    pre = await store.create(Task(id="", title="前置", status=Status.PENDING))
    task = await store.create(Task(id="", title="被阻塞"))
    await store.update(task, Patch(add_blocked_by=[pre]))

    tasks = await store.list_()
    t = next(t for t in tasks if t.id == task)
    assert t.is_ready is False  # 前置未完成

    await store.update(pre, Patch(status=Status.COMPLETED))
    tasks = await store.list_()
    t = next(t for t in tasks if t.id == task)
    assert t.is_ready is True


async def test_no_blockers_ready(tmp_path):
    store = Store(str(tmp_path / "tasks.json"))
    tid = await store.create(Task(id="", title="无依赖"))
    tasks = await store.list_()
    t = next(x for x in tasks if x.id == tid)
    assert t.is_ready is True  # 空 blocker 视为 ready（可开始）


async def test_update_unknown_id_returns_none(tmp_path):
    store = Store(str(tmp_path / "tasks.json"))
    assert await store.update("task_nope", Patch(status=Status.COMPLETED)) is None
