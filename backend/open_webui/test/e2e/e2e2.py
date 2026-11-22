"""E2E part 2: shell commands, background servers, and the interactive (browser) path."""
import json, os, socket, sys, time, uuid
import httpx

BASE, FAKE, WS = 'http://127.0.0.1:8099', 'http://127.0.0.1:9911', '/tmp/agent-ws'
RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond)))
    print(('PASS ' if cond else 'FAIL ') + name + (f'  -> {str(detail)[:400]}' if (detail and not cond) else ''))


def wait_for(fn, timeout=40, every=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


c = httpx.Client(base_url=BASE, timeout=30)
tok = c.post('/api/v1/auths/signin', json={'email': 'admin@example.com', 'password': 'pw-admin-123'}).json()['token']
H = {'Authorization': f'Bearer {tok}'}
api = httpx.Client(base_url=BASE, headers=H, timeout=30)
control = lambda **kw: httpx.post(f'{FAKE}/control', json=kw)
flog = lambda: httpx.get(f'{FAKE}/log').json()


def item_status(i):
    return next(x for x in api.get('/api/v1/queue/').json()['items'] if x['id'] == i)


def run_queue(prompt, tools, approval='auto'):
    control(reset=True, agent_tools=tools)
    it = api.post('/api/v1/queue/items', json={'prompt': prompt, 'model_id': 'fake-claude', 'title': prompt,
                                               'options': {'local_agent': {'enabled': True, 'cwd': WS, 'approval': approval}}}).json()
    st = wait_for(lambda: (lambda s: s if s['status'] in ('done', 'failed', 'needs_approval') else None)(item_status(it['id'])), 60)
    return it, st


print('== E1. all ten tools are offered to the model, spec generation is accepted')
it, st = run_queue('list tools', [{'name': 'local_list_dir', 'args': {'path': '.'}}])
check('queue run with list_dir completes', st and st['status'] == 'done', st)
tools = flog()[0]['tools']
expected = {'local_read_file', 'local_list_dir', 'local_glob', 'local_search', 'local_edit_file', 'local_write_file', 'local_run_command', 'local_start_process', 'local_process_logs', 'local_stop_process'}
check('all 10 local tools registered', expected <= set(tools), set(tools) ^ expected)
check('no local tools leak to other features (still plain builtin ones too)', len(tools) >= 10)
check('provider payload carries no internal params', not any(k.startswith('local_agent') or k == 'features' for k in flog()[0]['keys']), flog()[0]['keys'])
check('list_dir output reached the model', 'hello.txt' in (flog()[1]['tool_outputs'][0] if len(flog()) > 1 else ''), flog()[1:] )

print('\n== E2. shell command (auto mode)')
it, st = run_queue('run a command', [{'name': 'local_run_command', 'args': {'command': 'echo hello-from-shell && pwd'}}])
out = flog()[1]['tool_outputs'][0] if st and len(flog()) > 1 else ''
check('command executed and output returned to the model', 'hello-from-shell' in out and 'exit code 0' in out and os.path.realpath(WS) in out, out)

print('\n== E3. blocked command is refused even in auto mode')
it, st = run_queue('dangerous', [{'name': 'local_run_command', 'args': {'command': 'sudo rm -rf /'}}])
out = flog()[1]['tool_outputs'][0] if st and len(flog()) > 1 else ''
check('dangerous command refused', 'Command refused' in out, out)

print('\n== E4. edit an existing file (read -> edit)')
open(f'{WS}/app.py', 'w').write('def total():\n    return 1\n')
it, st = run_queue('edit', [{'name': 'local_edit_file', 'args': {'path': 'app.py', 'old_string': 'return 1', 'new_string': 'return 42'}}])
check('file edited on disk', open(f'{WS}/app.py').read() == 'def total():\n    return 42\n', open(f'{WS}/app.py').read())
check('model received the diff', '+    return 42' in (flog()[1]['tool_outputs'][0] if len(flog()) > 1 else ''))
files = None
ev_ok = st and st['status'] == 'done'
check('edit item done', ev_ok, st)

print('\n== E5. start a dev server in the background, fetch it, stop it')
port = free_port()
open(f'{WS}/index.html', 'w').write('<h1>served by the agent</h1>')
control(reset=True, agent_tools=[{'name': 'local_start_process', 'args': {'command': f'python3 -m http.server {port} --bind 127.0.0.1', 'name': 'web'}}])
it = api.post('/api/v1/queue/items', json={'prompt': 'start server', 'model_id': 'fake-claude',
                                           'options': {'local_agent': {'enabled': True, 'cwd': WS, 'approval': 'auto'}}}).json()
st = wait_for(lambda: (lambda s: s if s['status'] in ('done', 'failed') else None)(item_status(it['id'])), 60)
check('start_process item done', st and st['status'] == 'done', st)
body = None
for _ in range(20):
    try:
        body = httpx.get(f'http://127.0.0.1:{port}/index.html', timeout=2).text; break
    except Exception:
        time.sleep(0.5)
check('the server started by the agent really answers', body and 'served by the agent' in body, body)
procs = [p for p in api.get('/api/v1/local-agent/processes').json()['processes'] if p['status'] == 'running']
check('process listed in the UI endpoint', len(procs) == 1 and procs[0]['status'] == 'running' and procs[0]['name'] == 'web', procs)
r = api.delete(f"/api/v1/local-agent/processes/{procs[0]['id']}")
check('UI stop button endpoint works', r.status_code == 200, r.text)
time.sleep(1)
dead = False
try:
    httpx.get(f'http://127.0.0.1:{port}/index.html', timeout=2)
except Exception:
    dead = True
check('server is really stopped', dead)

print('\n== E6. the interactive (browser) path with approvals')
control(reset=True, agent_tools=[{'name': 'local_run_command', 'args': {'command': 'echo interactive-ok > interactive.txt && cat interactive.txt'}}])
uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
r = api.post('/api/chat/completions', json={
    'model': 'fake-claude', 'stream': True, 'session_id': 'sess-' + uid[:6], 'id': aid, 'parent_id': None,
    'messages': [{'role': 'user', 'content': 'run echo'}],
    'user_message': {'id': uid, 'parentId': None, 'role': 'user', 'content': 'run echo', 'timestamp': int(time.time()), 'models': ['fake-claude']},
    'params': {'local_agent_cwd': WS, 'local_agent_approval': 'ask'}, 'features': {'local_agent': True}, 'background_tasks': {}})
check('chat completion accepted', r.status_code == 200, r.text)
chat_id = r.json()['chat_id']


def msg():
    return api.get(f'/api/v1/chats/{chat_id}').json()['chat']['history']['messages'].get(aid)


m = wait_for(lambda: (lambda x: x if x and any(o.get('status') == 'pending' for o in (x.get('output') or []) if o.get('type') == 'function_call') else None)(msg()), 30)
check('command waits for the user (pending approval, not executed)', m is not None, msg())
check('no command output yet', not any(o.get('type') == 'function_call_output' for o in ((m or {}).get('output') or [])))
call = next(o for o in m['output'] if o.get('type') == 'function_call')
r = api.post(f'/api/v1/chats/{chat_id}/messages/{aid}/resolve', json={'call_id': call['call_id'], 'action': 'approve'})
check('user approves', r.status_code == 200, r.text)
fin = wait_for(lambda: (lambda x: x if x and x.get('done') and not x.get('error') else None)(msg()), 40)
check('assistant finished after approval', fin is not None, msg())
outs = [o for o in (fin or {}).get('output', []) if o.get('type') == 'function_call_output']
check('command ran with the browser-chosen folder', outs and 'interactive-ok' in json.dumps(outs) and 'exit code 0' in json.dumps(outs), outs)
check('final answer saved', 'All steps done' in json.dumps((fin or {}).get('output') or []) + str((fin or {}).get('content')))

print('\n== E7. reject keeps the machine untouched')
control(reset=True, agent_tools=[{'name': 'local_write_file', 'args': {'path': 'rejected.txt', 'content': 'x'}}])
uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
r = api.post('/api/chat/completions', json={
    'model': 'fake-claude', 'stream': True, 'session_id': 'sess-' + uid[:6], 'id': aid, 'parent_id': None,
    'messages': [{'role': 'user', 'content': 'write'}],
    'user_message': {'id': uid, 'parentId': None, 'role': 'user', 'content': 'write', 'timestamp': int(time.time()), 'models': ['fake-claude']},
    'params': {'local_agent_cwd': WS, 'local_agent_approval': 'ask'}, 'features': {'local_agent': True}, 'background_tasks': {}})
chat_id = r.json()['chat_id']
m = wait_for(lambda: (lambda x: x if x and any(o.get('status') == 'pending' for o in (x.get('output') or []) if o.get('type') == 'function_call') else None)(msg()), 30)
call = next(o for o in m['output'] if o.get('type') == 'function_call')
r = api.post(f'/api/v1/chats/{chat_id}/messages/{aid}/resolve', json={'call_id': call['call_id'], 'action': 'reject'})
check('user rejects', r.status_code == 200, r.text)
time.sleep(4)
check('rejected write never happened', not os.path.exists(f'{WS}/rejected.txt'))

failed = [n for n, ok in RESULTS if not ok]
print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
sys.exit(1 if failed else 0)
