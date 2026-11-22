import time

import pytest_asyncio

import _db_stub
from _load import load

_db_stub.install()
m = load('models/chat_queue.py', 'chat_queue_model')
Q = m.ChatQueue


@pytest_asyncio.fixture(autouse=True)
async def tables():
    async with _db_stub.engine.begin() as conn:
        await conn.run_sync(_db_stub.Base.metadata.drop_all)
        await conn.run_sync(_db_stub.Base.metadata.create_all)


def form(prompt, **kw):
    return m.ChatQueueItemForm(prompt=prompt, **kw)


async def test_claim_follows_priority_then_age():
    await Q.insert('u1', form('low', priority=500))
    await Q.insert('u1', form('high', priority=10))
    await Q.insert('u1', form('also-high-later', priority=10))
    order = []
    for _ in range(3):
        item = await Q.claim_next('u1')
        order.append(item.prompt)
        await Q.update_by_id(item.id, {'status': m.STATUS_DONE})
    assert order == ['high', 'also-high-later', 'low']
    assert await Q.claim_next('u1') is None


async def test_resume_items_jump_the_queue():
    await Q.insert('u1', form('normal'))
    r = await Q.insert('u1', form('resume', kind=m.KIND_RESUME))
    assert r.priority == m.RESUME_PRIORITY
    assert (await Q.claim_next('u1')).prompt == 'resume'


async def test_claim_is_exclusive_and_counts_attempts():
    await Q.insert('u1', form('only'))
    first = await Q.claim_next('u1')
    assert first.status == m.STATUS_RUNNING and first.attempts == 1
    assert await Q.claim_next('u1') is None  # already running


async def test_next_attempt_at_delays_item():
    item = await Q.insert('u1', form('later'))
    future = int(time.time()) + 3600
    await Q.update_by_id(item.id, {'next_attempt_at': future})
    assert await Q.claim_next('u1') is None
    assert await Q.user_ids_with_pending() == []
    assert (await Q.claim_next('u1', now=future + 1)).id == item.id


async def test_paused_items_are_skipped():
    item = await Q.insert('u1', form('p'))
    await Q.update_by_id(item.id, {'status': m.STATUS_PAUSED})
    assert await Q.claim_next('u1') is None


async def test_users_are_isolated():
    await Q.insert('u1', form('mine'))
    assert await Q.claim_next('u2') is None
    assert await Q.get_for_user('u2', (await Q.list_by_user('u1'))[0].id) is None
    assert await Q.user_ids_with_pending() == ['u1']


async def test_reorder_renumbers_in_given_order():
    a = await Q.insert('u1', form('a'))
    b = await Q.insert('u1', form('b'))
    c = await Q.insert('u1', form('c'))
    await Q.reorder('u1', [c.id, a.id, b.id, 'ghost'])
    assert [i.prompt for i in await Q.list_by_user('u1')] == ['c', 'a', 'b']
    assert [i.priority for i in await Q.list_by_user('u1')] == [10, 20, 30]


async def test_reorder_cannot_touch_other_users():
    a = await Q.insert('u1', form('a', priority=7))
    await Q.reorder('u2', [a.id])
    assert (await Q.get_by_id(a.id)).priority == 7


async def test_delete_and_cleanup():
    a = await Q.insert('u1', form('a'))
    b = await Q.insert('u1', form('b'))
    await Q.update_by_id(b.id, {'status': m.STATUS_DONE})
    assert not await Q.delete_by_id('u2', a.id)
    assert await Q.delete_finished('u1') == 1
    assert [i.id for i in await Q.list_by_user('u1')] == [a.id]
    assert await Q.delete_by_id('u1', a.id)


async def test_reset_interrupted_puts_running_back():
    await Q.insert('u1', form('a'))
    await Q.claim_next('u1')
    assert await Q.reset_interrupted() == 1
    assert (await Q.list_by_user('u1'))[0].status == m.STATUS_PENDING


async def test_count_by_status():
    a = await Q.insert('u1', form('a'))
    await Q.insert('u1', form('b'))
    await Q.update_by_id(a.id, {'status': m.STATUS_DONE})
    assert await Q.count_by_status('u1') == {'done': 1, 'pending': 1}


async def test_state_upsert_and_settings_merge():
    s = await Q.get_state('u1')
    assert s.paused is False and s.limit_until is None
    await Q.set_state('u1', paused=True, limit_until=123, limit_kind='usage_limit')
    s = await Q.get_state('u1')
    assert s.paused and s.limit_until == 123 and s.limit_kind == 'usage_limit'
    await Q.set_state('u1', settings={'auto_resume': True})
    merged = m.merged_settings(await Q.get_state('u1'))
    assert merged['auto_resume'] is True
    assert merged['notify_queue_done'] is True  # default preserved
