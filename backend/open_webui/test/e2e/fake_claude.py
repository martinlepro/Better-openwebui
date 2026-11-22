"""Fake OpenAI-compatible 'Claude' used for end-to-end tests.

Control (POST /control, JSON):
  {"limit_next": N, "retry_after": 6}   next N chat requests answer 429 + retry-after
  {"agent_file": "hello.txt"}            next request answers with a local_write_file tool call
  {"reset": true}
Introspection: GET /log -> every chat request received (tools offered, last messages)
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {'limit_next': 0, 'retry_after': 6, 'agent_file': None, 'agent_tools': [], 'log': [], 'lock': threading.Lock()}


def chunk(delta, finish=None):
    return 'data: ' + json.dumps({
        'id': 'chatcmpl-fake', 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': 'fake-claude',
        'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}],
    }) + '\n\n'


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, status, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, events):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        for e in events:
            payload = e.encode()
            self.wfile.write(f'{len(payload):x}\r\n'.encode() + payload + b'\r\n')
            self.wfile.flush()
            time.sleep(0.05)
        self.wfile.write(b'0\r\n\r\n')
        self.wfile.flush()

    def do_GET(self):
        if self.path.startswith('/v1/models'):
            return self._json(200, {'object': 'list', 'data': [{'id': 'fake-claude', 'object': 'model', 'created': 0, 'owned_by': 'fake'}]})
        if self.path == '/log':
            return self._json(200, STATE['log'])
        return self._json(404, {'error': 'nope'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b'{}'
        data = json.loads(raw or b'{}')
        if self.path == '/control':
            with STATE['lock']:
                if data.get('reset'):
                    STATE.update(limit_next=0, agent_file=None, agent_tools=[], log=[])
                for k in ('limit_next', 'retry_after', 'agent_file', 'agent_tools'):
                    if k in data:
                        STATE[k] = data[k]
            return self._json(200, {'ok': True})
        if not self.path.startswith('/v1/chat/completions'):
            return self._json(404, {'error': 'nope'})

        messages = data.get('messages') or []
        tools = [t['function']['name'] for t in (data.get('tools') or [])]
        with STATE['lock']:
            STATE['log'].append({'keys': sorted(data.keys()), 'tool_outputs': [m.get('content') for m in messages if m.get('role') == 'tool'], 'tools': tools, 'n_messages': len(messages), 'last': (messages[-1] if messages else None), 'roles': [m.get('role') for m in messages]})
            limited = STATE['limit_next'] > 0
            if limited:
                STATE['limit_next'] -= 1
            agent_file = STATE['agent_file']
            agent_tools = list(STATE['agent_tools'])
            retry_after = STATE['retry_after']

        if limited:
            return self._json(429, {'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'Usage limit reached for this window.'}},
                              {'retry-after': str(retry_after)})

        has_tool_result = any(m.get('role') == 'tool' for m in messages)
        stream = data.get('stream')
        round_no = sum(1 for m in messages if m.get('role') == 'tool')
        if agent_tools and round_no < len(agent_tools) and tools:
            spec = agent_tools[round_no]
            events = [
                chunk({'role': 'assistant', 'content': None, 'tool_calls': [{'index': 0, 'id': f'call_{round_no}', 'type': 'function', 'function': {'name': spec['name'], 'arguments': json.dumps(spec['args'])}}]}),
                chunk({}, 'tool_calls'),
                'data: [DONE]\n\n',
            ]
            return self._sse(events)
        if agent_tools and round_no >= len(agent_tools):
            return self._sse([chunk({'role': 'assistant', 'content': 'All steps done.'}), chunk({}, 'stop'), 'data: [DONE]\n\n'])
        if agent_file and 'local_write_file' in tools and not has_tool_result:
            args = json.dumps({'path': agent_file, 'content': 'written by the fake agent\n'})
            events = [
                chunk({'role': 'assistant', 'content': None, 'tool_calls': [{'index': 0, 'id': 'call_1', 'type': 'function', 'function': {'name': 'local_write_file', 'arguments': args}}]}),
                chunk({}, 'tool_calls'),
                'data: [DONE]\n\n',
            ]
        else:
            text = 'Done: I created the file.' if has_tool_result else 'Hello from fake Claude.'
            events = [chunk({'role': 'assistant', 'content': text}), chunk({}, 'stop'), 'data: [DONE]\n\n']

        if not stream:
            return self._json(200, {'id': 'x', 'object': 'chat.completion', 'created': 0, 'model': 'fake-claude',
                                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Hello from fake Claude.'}, 'finish_reason': 'stop'}]})
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        for e in events:
            payload = e.encode()
            self.wfile.write(f'{len(payload):x}\r\n'.encode() + payload + b'\r\n')
            self.wfile.flush()
            time.sleep(0.05)
        self.wfile.write(b'0\r\n\r\n')
        self.wfile.flush()


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 9911), Handler).serve_forever()
