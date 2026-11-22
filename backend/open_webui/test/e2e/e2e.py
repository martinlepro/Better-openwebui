"""End-to-end test of the chat queue + local agent against the REAL Open WebUI backend."""

import asyncio
import json
import os
import sys
import time
import uuid

import httpx
import socketio

BASE = 'http://127.0.0.1:8099'
FAKE = 'http://127.0.0.1:9911'
WS = '/tmp/agent-ws'
RESULTS = []
EVENTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond)))
    print(('PASS ' if cond else 'FAIL ') + name + (f'  -> {detail}' if (detail and not cond) else ''))


class Api:
    def __init__(self, token):
        self.c = httpx.Client(base_url=BASE, headers={'Authorization': f'Bearer {token}'}, timeout=30)

    def get(self, p, **kw):
        return self.c.get(p, **kw)

    def post(self, p, body=None):
        return self.c.post(p, json=body if body is not None else {})

    def patch(self, p, body):
        return self.c.patch(p, json=body)

    def put(self, p, body):
        return self.c.put(p, json=body)

    def queue(self):
        return self.get('/api/v1/queue/').json()


def control(**kw):
    httpx.post(f'{FAKE}/control', json=kw)


def fake_log():
    return httpx.get(f'{FAKE}/log').json()


def wait_for(fn, timeout=40, every=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def item(api, item_id):
    return next((i for i in api.queue()['items'] if i['id'] == item_id), None)


def events(kind):
    return [e for e in EVENTS if e['type'] == kind]


async def connect_socket(token):
    sio = socketio.AsyncClient()

    @sio.on('events')
    async def on_events(payload):
        data = payload.get('data') or {}
        EVENTS.append({'type': data.get('type'), 'data': data.get('data'), 't': time.time()})

    await sio.connect(BASE, socketio_path='/ws/socket.io', auth={'token': token}, transports=['websocket'])
    await sio.emit('user-join', {'auth': {'token': token}})
    await asyncio.sleep(1)
    return sio


def main_sync(token, admin_api, user_api):
    api = admin_api
    cfg = api.get('/api/config').json()
    check(
        'config exposes queue + local agent flags',
        cfg['features'].get('enable_chat_queue') is True and cfg['features'].get('enable_local_agent') is True,
        cfg['features'],
    )
    check(
        'non-admin does not get the local agent flag',
        user_api.get('/api/config').json()['features'].get('enable_local_agent') is False,
    )

    models = api.get('/api/models').json()['data']
    check('fake model is listed', any(m['id'] == 'fake-claude' for m in models), [m['id'] for m in models])

    # ---------------- A. limit -> pause -> automatic resume -----------------------------------
    print('\n== A. limit reached then automatic resume')
    control(reset=True, limit_next=1, retry_after=6)
    EVENTS.clear()
    t0 = time.time()
    r = api.post('/api/v1/queue/items', {'prompt': 'Say hi', 'model_id': 'fake-claude', 'title': 'E2E A'})
    check('create queue item', r.status_code == 200, r.text)
    a = r.json()
    st = wait_for(lambda: api.queue()['state'] if api.queue()['state']['limit_until'] else None, 20)
    check('limit detected from the real 429 (retry-after header)', st is not None)
    if st:
        wait_s = st['limit_until'] - t0
        check('reset time taken from retry-after (~6s, not a 5h default)', 4 <= wait_s <= 15, f'{wait_s:.1f}s')
        check('limit classified as a limit kind', st['limit_kind'] in ('rate_limit', 'usage_limit'), st['limit_kind'])
    cur = item(api, a['id'])
    check('item returned to pending (not failed) after the limit', cur['status'] == 'pending', cur)
    check(
        'queue:limit_reached notification sent once',
        len(events('queue:limit_reached')) == 1,
        len(events('queue:limit_reached')),
    )
    n_calls = len(fake_log())
    time.sleep(3)
    check('nothing is sent while the limit is active', len(fake_log()) == n_calls)

    done = wait_for(lambda: (lambda i: i if i['status'] == 'done' else None)(item(api, a['id'])), 40)
    check('item finished by itself after the limit', done is not None, item(api, a['id']))
    elapsed = time.time() - t0
    check('resumed no earlier than the reset', elapsed >= 5.5, f'{elapsed:.1f}s')
    log = fake_log()
    check('provider saw exactly 2 requests (429 then success)', len(log) == 2, len(log))
    chat = api.get(f'/api/v1/chats/{done["chat_id"]}').json()
    msgs = chat['chat']['history']['messages']
    users = [m for m in msgs.values() if m['role'] == 'user']
    asst = [m for m in msgs.values() if m['role'] == 'assistant']
    check(
        'question not duplicated on retry (1 user msg, 2 replies)',
        len(users) == 1 and len(asst) == 2,
        (len(users), len(asst)),
    )
    ok = [m for m in asst if not m.get('error')]
    check(
        'final reply saved in the conversation', ok and 'Hello from fake Claude' in (ok[0].get('content') or ''), asst
    )
    check('chat has the requested title', chat['title'] == 'E2E A', chat['title'])
    check(
        'queue:item_done notification with preview',
        any('fake Claude' in (e['data'].get('preview') or '') for e in events('queue:item_done')),
    )
    check('queue:queue_done notification once', len(events('queue:queue_done')) == 1, len(events('queue:queue_done')))
    check('limit_cleared event', len(events('queue:limit_cleared')) >= 1)

    # ---------------- B. priorities ------------------------------------------------------------
    print('\n== B. priorities')
    control(reset=True)
    EVENTS.clear()
    api.post('/api/v1/queue/pause')
    ids = {}
    for name, prio in (('LOW-job', 500), ('HIGH-job', 10), ('MID-job', 100)):
        ids[name] = api.post(
            '/api/v1/queue/items', {'prompt': name, 'model_id': 'fake-claude', 'priority': prio}
        ).json()['id']
    api.post(
        '/api/v1/queue/items/reorder', {'ids': [ids['MID-job'], ids['LOW-job'], ids['HIGH-job']]}
    )  # user drags: MID, LOW, HIGH
    order = [i['prompt'] for i in api.queue()['items'] if i['status'] == 'pending']
    check('reorder endpoint applies the new order', order == ['MID-job', 'LOW-job', 'HIGH-job'], order)
    api.post(f'/api/v1/queue/items/{ids["HIGH-job"]}/run-now')
    order = [i['prompt'] for i in api.queue()['items'] if i['status'] == 'pending']
    check('"run first" moves an item to the front', order[0] == 'HIGH-job', order)
    time.sleep(2.5)
    check('paused queue sends nothing', len(fake_log()) == 0, len(fake_log()))
    api.post('/api/v1/queue/resume')
    check('all three finish', wait_for(lambda: all(item(api, i)['status'] == 'done' for i in ids.values()), 40))
    sent = [l['last']['content'] for l in fake_log()]
    check('sent in priority order', sent == ['HIGH-job', 'MID-job', 'LOW-job'], sent)
    check(
        'item_done x3 then queue_done x1',
        len(events('queue:item_done')) == 3 and len(events('queue:queue_done')) == 1,
        [e['type'] for e in EVENTS],
    )

    # ---------------- C. interactive chat + button -----------------------------------------------
    print('\n== C. interactive chat hits the limit, user presses "resume automatically"')
    control(reset=True, limit_next=1, retry_after=6)
    EVENTS.clear()
    chat_id = interactive_chat(api, 'interactive hello')
    wait_for(lambda: api.queue()['state']['limit_until'], 15)
    st = api.queue()['state']
    check('limit from an interactive chat pauses the queue', st['limit_until'] is not None)
    check('no resume item yet (auto-resume is off)', not [i for i in api.queue()['items'] if i['chat_id'] == chat_id])
    r = api.post('/api/v1/queue/resume-chat', {'chat_id': chat_id})
    check('resume-chat button endpoint ok', r.status_code == 200, r.text)
    body = r.json()
    check(
        'button reads the limit from the failed reply',
        body['limit'] and body['limit']['kind'] in ('rate_limit', 'usage_limit'),
        body,
    )
    api.post('/api/v1/queue/resume-chat', {'chat_id': chat_id})
    resumes = [i for i in api.queue()['items'] if i['chat_id'] == chat_id and i['kind'] == 'resume']
    check('pressing twice queues only one resume', len(resumes) == 1, len(resumes))
    done = wait_for(lambda: (lambda i: i if i and i['status'] == 'done' else None)(item(api, resumes[0]['id'])), 40)
    check('interactive chat resumed automatically', done is not None, item(api, resumes[0]['id']))
    msgs = api.get(f'/api/v1/chats/{chat_id}').json()['chat']['history']['messages']
    users = [m for m in msgs.values() if m['role'] == 'user']
    asst = [m for m in msgs.values() if m['role'] == 'assistant' and not m.get('error')]
    check(
        'user question asked once, answered after the limit',
        len(users) == 1 and asst and 'fake Claude' in asst[0].get('content', ''),
        (len(users), asst),
    )

    # auto-resume setting ON -> created without pressing anything
    print('\n== C2. auto-resume setting')
    r = api.put('/api/v1/queue/settings', {'auto_resume': True, 'resume_message': 'Please continue.'})
    check('settings saved', r.status_code == 200 and r.json()['settings']['auto_resume'] is True, r.text)
    control(reset=True, limit_next=1, retry_after=6)
    chat2 = interactive_chat(api, 'second interactive')
    auto = wait_for(lambda: [i for i in api.queue()['items'] if i['chat_id'] == chat2 and i['kind'] == 'resume'], 15)
    check('resume item created automatically by the server', bool(auto))
    if auto:
        check('auto resume finishes too', wait_for(lambda: item(api, auto[0]['id'])['status'] == 'done', 40))
    api.put('/api/v1/queue/settings', {'auto_resume': False})

    # ---------------- D. local agent -------------------------------------------------------------
    print('\n== D. local agent (edit files like Claude Code)')
    for f in ('hello.txt', 'ask.txt'):
        if os.path.exists(f'{WS}/{f}'):
            os.remove(f'{WS}/{f}')
    control(reset=True, agent_file='hello.txt')
    EVENTS.clear()
    r = api.post(
        '/api/v1/queue/items',
        {
            'prompt': 'create hello.txt',
            'model_id': 'fake-claude',
            'title': 'Agent auto',
            'options': {'local_agent': {'enabled': True, 'cwd': WS, 'approval': 'auto'}},
        },
    )
    check('create local-agent item (admin)', r.status_code == 200, r.text)
    d = r.json()
    check(
        'item stores validated cwd/approval',
        d['options']['local_agent']['cwd'] == os.path.realpath(WS)
        and d['options']['local_agent']['approval'] == 'auto',
        d['options'],
    )
    done = wait_for(
        lambda: (lambda i: i if i['status'] in ('done', 'failed', 'needs_approval') else None)(item(api, d['id'])), 40
    )
    check('agent item reached done', done and done['status'] == 'done', done)
    check(
        'file really created on disk by the tool',
        os.path.exists(f'{WS}/hello.txt') and open(f'{WS}/hello.txt').read() == 'written by the fake agent\n',
    )
    tools = fake_log()[0]['tools']
    check('model was offered the local tools', 'local_write_file' in tools and 'local_run_command' in tools, tools)
    sysmsg = [m for m in [fake_log()[0]['last']]]
    notif = events('queue:item_done')
    check(
        'notification lists the changed file',
        notif and notif[-1]['data'].get('files') == ['hello.txt'],
        notif[-1]['data'] if notif else None,
    )

    print('\n== D2. approval flow (ask mode) while "away"')
    control(reset=True, agent_file='ask.txt')
    EVENTS.clear()
    d2 = api.post(
        '/api/v1/queue/items',
        {
            'prompt': 'create ask.txt',
            'model_id': 'fake-claude',
            'title': 'Agent ask',
            'options': {'local_agent': {'enabled': True, 'cwd': WS, 'approval': 'ask'}},
        },
    ).json()
    st = wait_for(
        lambda: (lambda i: i if i['status'] in ('needs_approval', 'done', 'failed') else None)(item(api, d2['id'])), 30
    )
    check('item waits for approval instead of acting', st and st['status'] == 'needs_approval', st)
    check('file NOT created before approval', not os.path.exists(f'{WS}/ask.txt'))
    check('approval notification sent', len(events('queue:needs_approval')) == 1)
    msg = api.get(f'/api/v1/chats/{st["chat_id"]}').json()['chat']['history']['messages'][st['result_message_id']]
    calls = [o for o in (msg.get('output') or []) if o.get('type') == 'function_call']
    check(
        'pending call is visible to the user',
        calls and calls[0]['name'] == 'local_write_file' and calls[0].get('status') == 'pending',
        calls,
    )
    r = api.post(
        f'/api/v1/chats/{st["chat_id"]}/messages/{st["result_message_id"]}/resolve',
        {'call_id': calls[0]['call_id'], 'action': 'approve'},
    )
    check('approve endpoint accepts', r.status_code == 200, r.text)
    fin = wait_for(lambda: (lambda i: i if i['status'] == 'done' else None)(item(api, d2['id'])), 40)
    check(
        'after approval the action runs and the item completes',
        fin is not None and os.path.exists(f'{WS}/ask.txt'),
        item(api, d2['id']),
    )

    print('\n== D3. security')
    r = user_api.post(
        '/api/v1/queue/items',
        {'prompt': 'x', 'model_id': 'fake-claude', 'options': {'local_agent': {'enabled': True, 'cwd': WS}}},
    )
    check('non-admin cannot enable the local agent', r.status_code == 403, f'{r.status_code} {r.text}')
    r = api.post(
        '/api/v1/queue/items',
        {'prompt': 'x', 'model_id': 'fake-claude', 'options': {'local_agent': {'enabled': True, 'cwd': '/etc'}}},
    )
    check('cwd outside the allowed folders rejected', r.status_code == 400, f'{r.status_code} {r.text}')
    check(
        'validate-cwd endpoint',
        api.post('/api/v1/local-agent/validate-cwd', {'path': '/etc'}).status_code == 400
        and api.post('/api/v1/local-agent/validate-cwd', {'path': WS}).status_code == 200,
    )
    control(reset=True, agent_file='../escape.txt')
    d3 = api.post(
        '/api/v1/queue/items',
        {
            'prompt': 'escape',
            'model_id': 'fake-claude',
            'options': {'local_agent': {'enabled': True, 'cwd': WS, 'approval': 'auto'}},
        },
    ).json()
    wait_for(lambda: item(api, d3['id'])['status'] in ('done', 'failed'), 40)
    check('path traversal blocked: nothing written outside the workspace', not os.path.exists('/tmp/escape.txt'))
    other = user_api.get('/api/v1/queue/').json()
    check('queues are private per user', other['items'] == [])
    check(
        'non-admin sees local agent as unavailable',
        user_api.get('/api/v1/local-agent/info').json()['available'] is False,
    )
    check('non-admin cannot read admin config', user_api.get('/api/v1/local-agent/config').status_code in (401, 403))
    cfg = api.get('/api/v1/local-agent/config').json()
    check('admin config readable', cfg['enable'] is True and cfg['allowed_roots'] == [os.path.realpath(WS)], cfg)
    r = api.post('/api/v1/local-agent/config', {'allowed_roots': ['/']})
    check('filesystem root refused as allowed folder', r.status_code == 400, r.text)


def interactive_chat(api, text):
    """What the browser does for a brand new chat."""
    uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
    r = api.post(
        '/api/chat/completions',
        {
            'model': 'fake-claude',
            'stream': True,
            'session_id': 'sess-' + uid[:6],
            'id': aid,
            'parent_id': None,
            'messages': [{'role': 'user', 'content': text}],
            'user_message': {
                'id': uid,
                'parentId': None,
                'role': 'user',
                'content': text,
                'timestamp': int(time.time()),
                'models': ['fake-claude'],
            },
            'params': {},
            'features': {},
            'background_tasks': {},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()['chat_id']


async def main():
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.post('/api/v1/auths/signup', json={'name': 'Admin', 'email': 'admin@example.com', 'password': 'pw-admin-123'})
    if r.status_code != 200:  # already created by a previous run
        r = c.post('/api/v1/auths/signin', json={'email': 'admin@example.com', 'password': 'pw-admin-123'})
    assert r.status_code == 200, r.text
    admin_token = r.json()['token']
    r = c.post(
        '/api/v1/auths/add',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={'name': 'Bob', 'email': 'bob@example.com', 'password': 'pw-bob-12345', 'role': 'user'},
    )
    if r.status_code != 200:  # already exists
        r = c.post('/api/v1/auths/signin', json={'email': 'bob@example.com', 'password': 'pw-bob-12345'})
    assert r.status_code == 200, r.text
    user_token = r.json()['token']
    admin, user = Api(admin_token), Api(user_token)
    sio = await connect_socket(admin_token)
    try:
        await asyncio.get_event_loop().run_in_executor(None, main_sync, admin_token, admin, user)
    finally:
        await sio.disconnect()
    failed = [n for n, ok in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    if failed:
        print('FAILED:', failed)
        sys.exit(1)


asyncio.run(main())
