"""Task lifecycle, pause/cancel, and the 'wait 10 s then continue from the same step' guarantee."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from cr_analyst.agent.tasks import (CANCELLED, COMPLETED, EXECUTING, FAILED, PAUSED, PLANNING, QUEUED,
                                    RECOVERY_REQUIRED, InvalidTransition, PlanStep, TaskCancelled, TaskManager,
                                    can_transition)


async def make(tm, **kw):
    plan = [PlanStep("plan", None), PlanStep("a", "t1"), PlanStep("b", "t2"), PlanStep("verify", None)]
    return await tm.create(1, 100, "scan", "goal", plan=plan, **kw)


def test_transition_table():
    assert can_transition(QUEUED, PLANNING) and can_transition(EXECUTING, COMPLETED)
    assert not can_transition(COMPLETED, QUEUED) and not can_transition(CANCELLED, EXECUTING)
    assert can_transition(RECOVERY_REQUIRED, QUEUED) and can_transition(FAILED, QUEUED)


async def test_lifecycle_and_invalid_transition(udb):
    tm = TaskManager(udb)
    tv = await make(tm)
    assert tv.status == QUEUED and tv.required_tools == ["t1", "t2"]
    for st in (PLANNING, EXECUTING, COMPLETED):
        tv = await tm.transition(tv.id, st)
    assert tv.status == COMPLETED and tv.progress == 100
    with pytest.raises(InvalidTransition):
        await tm.transition(tv.id, EXECUTING)
    events = [e.event for e in await tm.events(tv.id)]
    assert events[0] == "created" and "status:COMPLETED" in events


async def test_claim_is_atomic(udb):
    tm = TaskManager(udb)
    tv = await make(tm)
    results = await asyncio.gather(tm.claim_next(), tm.claim_next(), tm.claim_next())
    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1 and claimed[0].id == tv.id and claimed[0].status == PLANNING


async def test_cancel_and_pause(udb):
    tm = TaskManager(udb)
    q = await make(tm)
    assert await tm.request_pause(q.id) and (await tm.get(q.id)).status == PAUSED
    assert await tm.resume(q.id) and (await tm.get(q.id)).status == QUEUED
    assert await tm.request_cancel(q.id) and (await tm.get(q.id)).status == CANCELLED
    assert not await tm.request_cancel(q.id)  # terminal
    r = await make(tm)
    await tm.transition(r.id, EXECUTING)
    assert not await tm.request_cancel(r.id, user_id=999)  # other user's task
    assert await tm.request_cancel(r.id, user_id=1)
    with pytest.raises(TaskCancelled):
        await tm.checkpoint_guard(r.id)


async def test_restart_recovery_resumes_same_step_after_delay(udb):
    tm = TaskManager(udb)
    tv = await make(tm)
    await tm.transition(tv.id, EXECUTING)
    await tm.step(tv.id, "plan", "done")
    await tm.step(tv.id, "a", "done")
    await tm.checkpoint_result(tv.id, {"a": {"value": 1}})
    await tm.step(tv.id, "b", "running")
    # --- worker dies here ---
    tm2 = TaskManager(udb)
    rec = await tm2.interrupted_to_recovery()
    assert [r.id for r in rec] == [tv.id] and (await tm2.get(tv.id)).status == RECOVERY_REQUIRED
    requeued, failed = await tm2.requeue_recoverable(max_attempts=5, delay_seconds=10)
    assert [r.id for r in requeued] == [tv.id] and not failed
    assert await tm2.resume_step(tv.id) == "b"                    # continues from the same step
    assert await tm2.partial(tv.id) == {"a": {"value": 1}}       # completed step result kept
    assert await tm2.claim_next() is None                        # still inside the 10 s resume delay
    after = datetime.fromisoformat((await tm2.get(tv.id)).params["_resume_after"])
    assert timedelta(seconds=8) < after - datetime.now(timezone.utc) <= timedelta(seconds=10)
    await tm2.schedule_resume(tv.id, 0)
    claimed = await tm2.claim_next()
    assert claimed and claimed.id == tv.id


async def test_recovery_gives_up_after_max_attempts(udb):
    tm = TaskManager(udb)
    tv = await make(tm)
    for _ in range(3):
        await tm.transition(tv.id, EXECUTING)
        await tm.interrupted_to_recovery()
        await tm.requeue_recoverable(max_attempts=3, delay_seconds=0)
    await tm.transition(tv.id, EXECUTING)
    await tm.interrupted_to_recovery()
    requeued, failed = await tm.requeue_recoverable(max_attempts=3, delay_seconds=0)
    assert not requeued and [f.id for f in failed] == [tv.id]


async def test_orchestrator_step_waits_and_resumes_same_step(services, monkeypatch):
    """In-process: a transient error inside step 'b' → notify, wait RESUME_DELAY, retry 'b' (not 'a')."""
    svc = services
    svc.settings.resume_delay_seconds = 0
    orch = svc.orchestrator
    published = []

    async def fake_publish(lang, tv, **kw):
        published.append(kw.get("op") or kw.get("note") or "")
    monkeypatch.setattr(svc.notifier, "publish", fake_publish)
    user = (await svc.users.register(42, "u", "U", "en", 42, "private", ("en",))).user
    tv = await svc.tasks.create(user.id, 42, "scan", "g",
                                plan=[PlanStep("plan", None), PlanStep("api_scan", "x"), PlanStep("interpret", None)])
    calls = {"api_scan": 0, "interpret": 0}

    async def step_a():
        calls["api_scan"] += 1
        return {"ok": 1}

    async def step_b():
        calls["interpret"] += 1
        if calls["interpret"] == 1:
            raise httpx.ConnectError("model connection dropped")
        return {"text": "done"}

    assert await orch._step(tv, "en", "api_scan", 40, step_a) == {"ok": 1}
    assert await orch._step(tv, "en", "interpret", 80, step_b) == {"text": "done"}
    assert calls == {"api_scan": 1, "interpret": 2}
    assert any("same step" in p.lower() or "interrupted" in p.lower() for p in published)
    # a re-run (e.g. after restart) restores both steps from the checkpoint instead of executing them again
    assert await orch._step(tv, "en", "api_scan", 40, step_a) == {"ok": 1}
    assert await orch._step(tv, "en", "interpret", 80, step_b) == {"text": "done"}
    assert calls == {"api_scan": 1, "interpret": 2}
    evs = [e.event for e in await svc.tasks.events(tv.id, 100)]
    assert "interrupted" in evs


async def test_permanent_error_is_not_retried(services, monkeypatch):
    svc = services
    async def nop(*a, **k):
        return None
    monkeypatch.setattr(svc.notifier, "publish", nop)
    user = (await svc.users.register(43, "u", "U", "en", 43, "private", ("en",))).user
    tv = await svc.tasks.create(user.id, 43, "scan", "g", plan=[PlanStep("api_scan", "x")])
    n = {"c": 0}

    async def bad():
        n["c"] += 1
        raise ValueError("permanent")
    with pytest.raises(ValueError):
        await svc.orchestrator._step(tv, "en", "api_scan", 40, bad)
    assert n["c"] == 1
