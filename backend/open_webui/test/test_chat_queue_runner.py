import asyncio
import sys
import time

import pytest_asyncio

import _db_stub
from _fake_app import World, build

world = World()
runner, m, app = build(world)
Q = m.ChatQueue
_FAKE_CONFIG = sys.modules['open_webui.models.config']


@pytest_asyncio.fixture(autouse=True)
async def fresh():
    sys.modules['open_webui.models.config'] = _FAKE_CONFIG  # other test files install their own
    async with _db_stub.engine.begin() as conn:
        await conn.run_sync(_db_stub.Base.metadata.drop_all)
        await conn.run_sync(_db_stub.Base.metadata.create_all)
    world.__init__()
    app.state.CHAT_COMPLETION_HANDLER = world.handler
    runner._running_users.clear()
    runner._limit_registry.clear()
    yield


async def drain(timeout=5):
    """Wait for the background run tasks started by _tick."""
    start = time.monotonic()
    while runner._running_users:
        assert time.monotonic() - start < timeout, 'runner did not finish'
        await asyncio.sleep(0.01)


async def tick():
    await runner._tick(app)
    await drain()


def add(prompt, **kw):
    return Q.insert('u1', m.ChatQueueItemForm(prompt=prompt, model_id=kw.pop('model_id', 'claude'), **kw))


def kinds():
    return [e[1] for e in world.events]


async def expire_limit():
    await Q.set_state('u1', limit_until=int(time.time()) - 1)


# ----------------------------------------------------------------------------------


async def test_new_conversation_runs_and_notifies():
    item = await add('Refactor the parser', title='Parser')
    await tick()
    done = await Q.get_by_id(item.id)
    assert done.status == m.STATUS_DONE and done.chat_id
    chat = world.chat(done.chat_id)
    assert chat['title'] == 'Parser'
    assert len(world.handler_calls) == 1
    assert world.handler_calls[0]['session_id'] == f'queue:{item.id}'
    assert [k for k in kinds() if k in ('queue:item_done', 'queue:queue_done')] == [
        'queue:item_done',
        'queue:queue_done',
    ]
    payload = next(e[2] for e in world.events if e[1] == 'queue:item_done')
    assert payload['title'] == 'Parser' and 'All done.' in payload['preview']
    summary = next(e[2] for e in world.events if e[1] == 'queue:queue_done')
    assert summary['done'] == 1 and summary['failed'] == 0


async def test_priority_order_and_single_queue_done():
    await add('low', priority=500, title='low')
    await add('urgent', priority=1, title='urgent')
    await add('mid', priority=50, title='mid')
    order = []
    for _ in range(3):
        await runner._tick(app)
        await drain()
    for call in world.handler_calls:
        order.append(call['user_message']['content'])
    assert order == ['urgent', 'mid', 'low']
    assert kinds().count('queue:item_done') == 3
    assert kinds().count('queue:queue_done') == 1  # only once everything is finished
    assert kinds().index('queue:queue_done') > max(i for i, k in enumerate(kinds()) if k == 'queue:item_done')


async def test_limit_pauses_queue_then_resumes_without_duplicating_the_message():
    item = await add('Build the feature', title='Feature')
    other = await add('Second job', title='Second', priority=200)
    world.script = [
        {'kind': 'error', 'error': 'rate_limit_error: Usage limit reached. Try again in 2 hours.'},
        {'kind': 'ok', 'text': 'Feature built.'},
    ]

    await tick()
    first = await Q.get_by_id(item.id)
    assert first.status == m.STATUS_PENDING and 'limit' in first.last_error.lower()
    state = await Q.get_state('u1')
    assert state.limit_until and state.limit_until > time.time() + 3000  # ~2h
    assert kinds().count('queue:limit_reached') == 1
    limit_event = next(e[2] for e in world.events if e[1] == 'queue:limit_reached')
    assert limit_event['known_reset'] is True and limit_event['pending'] >= 1

    # While the limit is active nothing is sent, even across many polls.
    for _ in range(3):
        await tick()
    assert len(world.handler_calls) == 1
    assert kinds().count('queue:limit_reached') == 1  # no notification spam

    # The limit is over: the same conversation resumes first, by retrying (no new user message).
    await expire_limit()
    await tick()
    assert 'queue:limit_cleared' in kinds()
    retried = await Q.get_by_id(item.id)
    assert retried.status == m.STATUS_DONE and retried.attempts == 2
    msgs = world.messages(retried.chat_id)
    users = [x for x in msgs.values() if x['role'] == 'user']
    assistants = [x for x in msgs.values() if x['role'] == 'assistant']
    assert len(users) == 1 and len(assistants) == 2  # one question, failed reply + new reply
    assert world.handler_calls[1]['user_message']['id'] == users[0]['id']

    # Then the second item runs and the queue completes exactly once.
    await tick()
    assert (await Q.get_by_id(other.id)).status == m.STATUS_DONE
    assert kinds().count('queue:queue_done') == 1


async def test_limit_with_partial_work_continues_with_a_new_message():
    item = await add('Long task', title='Long')
    world.script = [
        {'kind': 'error', 'error': 'Too many requests', 'partial': 'I changed 3 files so far'},
        {'kind': 'ok'},
    ]
    await tick()
    await expire_limit()
    await tick()
    second = world.handler_calls[1]
    assert second['user_message']['content'] == m.DEFAULT_RESUME_MESSAGE
    done = await Q.get_by_id(item.id)
    msgs = world.messages(done.chat_id)
    assert len([x for x in msgs.values() if x['role'] == 'user']) == 2
    assert second['parent_id']  # attached to the interrupted reply


async def test_user_paused_queue_does_not_run():
    await add('x')
    await Q.set_state('u1', paused=True)
    await tick()
    assert world.handler_calls == []


async def test_non_limit_error_fails_item_and_notifies():
    item = await add('boom', title='Boom')
    world.script = [{'kind': 'error', 'error': 'Invalid API key'}]
    await tick()
    failed = await Q.get_by_id(item.id)
    assert failed.status == m.STATUS_FAILED and 'Invalid API key' in failed.last_error
    assert 'queue:item_failed' in kinds()
    summary = next(e[2] for e in world.events if e[1] == 'queue:queue_done')
    assert summary['failed'] == 1


async def test_notification_preferences_are_respected():
    await Q.set_state('u1', settings={'notify_item_done': False})
    await add('quiet')
    await tick()
    assert 'queue:item_done' not in kinds()
    assert 'queue:queue_done' in kinds()


async def test_webhook_receives_notifications_when_enabled():
    world.config['ui.enable_user_webhooks'] = True
    await add('hook me', title='Hook')
    await tick()
    titles = [w[1] for w in world.webhooks]
    assert 'Conversation terminée' in titles and 'File d’attente terminée' in titles


async def test_busy_chat_is_not_interleaved():
    world.add_chat('c1')
    world.add_exchange('c1')
    world.busy_chats.add('c1')
    item = await add('follow up', chat_id='c1')
    await tick()
    waiting = await Q.get_by_id(item.id)
    assert waiting.status == m.STATUS_PENDING and waiting.next_attempt_at > time.time()
    assert waiting.attempts == 0 and world.handler_calls == []


async def test_message_is_appended_to_existing_conversation():
    world.add_chat('c1')
    _, aid = world.add_exchange('c1', assistant={'content': 'earlier answer'})
    item = await add('and now add tests', chat_id='c1')
    await tick()
    call = world.handler_calls[0]
    assert call['chat_id'] == 'c1' and call['parent_id'] == aid
    assert (await Q.get_by_id(item.id)).status == m.STATUS_DONE


async def test_needs_approval_then_finishes_when_user_approves():
    item = await add('delete stuff', title='Cleanup')
    world.script = [{'kind': 'approval'}]
    await tick()
    waiting = await Q.get_by_id(item.id)
    assert waiting.status == m.STATUS_NEEDS_APPROVAL
    assert 'queue:needs_approval' in kinds() and 'queue:queue_done' not in kinds()

    # The user approves in the chat UI: the reply finishes outside the queue.
    reply = world.messages(waiting.chat_id)[waiting.result_message_id]
    reply.update(done=True, content='Cleaned.', output=[])
    await runner._tick(app)
    assert (await Q.get_by_id(item.id)).status == m.STATUS_DONE
    assert 'queue:queue_done' in kinds()


async def test_changed_files_are_reported_for_local_agent_runs():
    output = [
        {
            'type': 'function_call',
            'name': 'local_edit_file',
            'status': 'completed',
            'arguments': '{"path": "src/a.py"}',
        },
        {
            'type': 'function_call',
            'name': 'local_write_file',
            'status': 'completed',
            'arguments': '{"path": "src/b.py", "content": "x"}',
        },
        {'type': 'function_call', 'name': 'local_run_command', 'status': 'completed', 'arguments': '{"command": "ls"}'},
    ]
    world.script = [{'kind': 'ok', 'output': output}]
    await add('edit things')
    await tick()
    payload = next(e[2] for e in world.events if e[1] == 'queue:item_done')
    assert payload['files'] == ['src/a.py', 'src/b.py']


async def test_local_agent_options_reach_the_request():
    await add('agent', options={'local_agent': {'enabled': True, 'cwd': '/work/app', 'approval': 'edits'}})
    await tick()
    call = world.handler_calls[0]
    assert call['features']['local_agent'] is True
    assert call['params']['local_agent_cwd'] == '/work/app'
    assert call['params']['local_agent_approval'] == 'edits'


async def test_missing_model_fails_cleanly():
    item = await Q.insert('u1', m.ChatQueueItemForm(prompt='no model'))
    await tick()
    failed = await Q.get_by_id(item.id)
    assert failed.status == m.STATUS_FAILED and 'model' in failed.last_error.lower()


async def test_attempt_cap_stops_endless_limit_loops():
    runner.MAX_ATTEMPTS = 2
    try:
        item = await add('stubborn')
        world.script = [{'kind': 'error', 'error': 'rate limit'}] * 5
        for _ in range(4):
            await tick()
            await expire_limit()
        assert (await Q.get_by_id(item.id)).status == m.STATUS_FAILED
    finally:
        runner.MAX_ATTEMPTS = 25


# --- interactive hook ---------------------------------------------------------------


class Req:
    def __init__(self):
        self.app = app


async def test_hook_pauses_queue_and_queues_resume_when_auto_resume_on():
    world.add_chat('c1')
    world.add_exchange('c1', assistant={'error': {'content': 'limit'}, 'content': ''})
    await Q.set_state('u1', settings={'auto_resume': True})
    user = world.users['u1']
    meta = {'chat_id': 'c1', 'message_id': 'm1', 'session_id': 'abc'}
    await runner.on_chat_error(Req(), user, meta, 'Usage limit reached. Try again in 3 hours.', status=429)
    await runner.on_chat_error(
        Req(), user, meta, 'Usage limit reached. Try again in 3 hours.', status=429
    )  # idempotent
    items = await Q.list_by_user('u1')
    assert len(items) == 1 and items[0].kind == m.KIND_RESUME and items[0].chat_id == 'c1'
    assert (await Q.get_state('u1')).limit_until > time.time() + 3 * 3600 - 60
    assert kinds().count('queue:limit_reached') == 1


async def test_hook_without_auto_resume_only_records_the_limit():
    world.add_chat('c1')
    await runner.on_chat_error(
        Req(), world.users['u1'], {'chat_id': 'c1', 'message_id': 'm1'}, 'rate_limit_error', status=429
    )
    assert await Q.list_by_user('u1') == []
    assert (await Q.get_state('u1')).limit_until


async def test_hook_ignores_non_limit_errors_and_queue_runs():
    await Q.set_state('u1', settings={'auto_resume': True})
    user = world.users['u1']
    await runner.on_chat_error(Req(), user, {'chat_id': 'c1', 'message_id': 'm'}, 'Invalid API key', status=401)
    await runner.on_chat_error(
        Req(), user, {'chat_id': 'c1', 'message_id': 'm', 'session_id': 'queue:x'}, 'rate limit', status=429
    )
    assert await Q.list_by_user('u1') == []
    assert (await Q.get_state('u1')).limit_until is None


async def test_hook_never_raises():
    class Broken:
        @property
        def app(self):
            raise RuntimeError('boom')

    await runner.on_chat_error(
        Broken(), world.users['u1'], {'chat_id': 'c1', 'message_id': 'm'}, 'rate limit', status=429
    )


async def test_resume_item_skips_when_nothing_to_resume():
    world.add_chat('c1')
    world.add_exchange('c1', assistant={'content': 'fine', 'done': True})
    item = await Q.insert(
        'u1', m.ChatQueueItemForm(prompt='continue', chat_id='c1', kind=m.KIND_RESUME, model_id='claude')
    )
    await tick()
    assert world.handler_calls == []
    done = await Q.get_by_id(item.id)
    assert done.status == m.STATUS_DONE and done.last_error == 'already answered'


async def test_resume_retries_when_last_reply_failed_empty():
    world.add_chat('c1')
    uid, aid = world.add_exchange(
        'c1', user_text='original question', assistant={'error': {'content': 'limit'}, 'content': '', 'done': True}
    )
    item = await Q.insert('u1', m.ChatQueueItemForm(prompt='continue', chat_id='c1', kind=m.KIND_RESUME))
    await tick()
    call = world.handler_calls[0]
    assert call['user_message']['id'] == uid  # regenerated, not re-asked
    assert call['model'] == 'claude'  # taken from the chat
    assert (await Q.get_by_id(item.id)).status == m.STATUS_DONE


# --- pure helpers -------------------------------------------------------------------


def test_plan_send_variants():
    mk = lambda **kw: m.ChatQueueItemModel(id='i', user_id='u', prompt='p', created_at=1, updated_at=1, **kw)
    assert runner.plan_send(None, mk()) == {'mode': 'append', 'parent_id': None, 'text': 'p'}
    hist = {
        'currentId': 'a',
        'messages': {
            'u': {'id': 'u', 'role': 'user'},
            'a': {'id': 'a', 'role': 'assistant', 'parentId': 'u', 'done': True},
        },
    }
    assert runner.plan_send(hist, mk()) == {'mode': 'append', 'parent_id': 'a', 'text': 'p'}
    assert runner.plan_send({'messages': {}, 'currentId': None}, mk(kind='resume'))['mode'] == 'skip'


def test_make_title_and_preview_helpers():
    assert runner.make_title('  Fix the bug\nmore detail') == 'Fix the bug'
    assert runner.make_title('') == 'Queued chat'
    assert len(runner.make_title('x' * 200)) <= 60
    out = {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'hello'}]}]}
    assert runner.reply_text(out) == 'hello'


def test_render_text_languages():
    data = {'reset_at': time.time() + 7200, 'wait_seconds': 7200, 'pending': 3}
    assert 'Reprise automatique' in runner.render_text('limit_reached', data, 'fr')[1]
    assert 'Resuming automatically' in runner.render_text('limit_reached', data, 'en')[1]


# --- exact reset times beat guesses ---------------------------------------------------


def info_of(runner_mod, **kw):
    return runner_mod.LimitInfo(kind=kw.pop('kind', 'usage_limit'), message='limit', **kw)


async def test_exact_reset_replaces_an_earlier_guess():
    guess = info_of(runner, reset_at=None)  # text only: probe schedule (10 min)
    exact = info_of(runner, reset_at=time.time() + 30)  # provider said 30 s
    await runner.register_limit(app, 'u1', guess)
    guessed = (await Q.get_state('u1')).limit_until
    assert guessed > time.time() + 500 and not (await Q.get_state('u1')).limit_exact
    await runner.register_limit(app, 'u1', exact)
    state = await Q.get_state('u1')
    assert state.limit_exact and state.limit_until < time.time() + 60
    assert kinds().count('queue:limit_reached') == 1  # still a single notification


async def test_a_guess_never_replaces_an_exact_reset():
    exact = info_of(runner, reset_at=time.time() + 120)
    await runner.register_limit(app, 'u1', exact)
    until = (await Q.get_state('u1')).limit_until
    await runner.register_limit(app, 'u1', info_of(runner, reset_at=None))
    state = await Q.get_state('u1')
    assert state.limit_until == until and state.limit_exact


async def test_two_exact_limits_keep_the_later_one():
    await runner.register_limit(app, 'u1', info_of(runner, reset_at=time.time() + 100))
    await runner.register_limit(app, 'u1', info_of(runner, reset_at=time.time() + 3000))
    await runner.register_limit(app, 'u1', info_of(runner, reset_at=time.time() + 50))
    assert (await Q.get_state('u1')).limit_until > time.time() + 2900


async def test_unknown_reset_is_probed_not_waited_5_hours():
    await add('probe me')
    world.script = [{'kind': 'error', 'error': 'Usage limit reached for this window.'}]
    await tick()
    state = await Q.get_state('u1')
    assert not state.limit_exact
    assert 500 < state.limit_until - time.time() <= 1800  # ~10 minutes, not ~5 hours
