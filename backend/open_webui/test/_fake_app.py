"""Fakes for the Open WebUI pieces the chat queue runner talks to (tests only)."""

import sys
import types
from types import SimpleNamespace
from uuid import uuid4

import _db_stub
from _load import load


def _pkg(name):
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
    return module


def _mod(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class World:
    """One fake deployment: chats, users, config, emitted events, scripted model outcomes."""

    def __init__(self):
        self.config = {'queue.enable': True, 'ui.enable_user_webhooks': False, 'memories.enable': False}
        self.chats = {}
        self.events = []  # (room, event_type, data)
        self.webhooks = []
        self.handler_calls = []
        self.script = []  # outcomes consumed by the fake chat handler
        self.busy_chats = set()
        self.users = {'u1': SimpleNamespace(id='u1', name='Ann', role='user', settings=None)}

    # --- chat store -----------------------------------------------------------------
    def chat(self, chat_id):
        return self.chats.get(chat_id)

    def messages(self, chat_id):
        return self.chats[chat_id]['chat']['history']['messages']

    def leaf(self, chat_id):
        return self.chats[chat_id]['chat']['history']['currentId']

    def add_chat(self, chat_id='c1', user_id='u1', title='Existing', models=('claude',)):
        self.chats[chat_id] = {
            'id': chat_id,
            'user_id': user_id,
            'title': title,
            'chat': {'models': list(models), 'history': {'messages': {}, 'currentId': None}},
        }
        return self.chats[chat_id]

    def add_exchange(self, chat_id, *, user_text='hi', assistant=None):
        """Append user+assistant messages (assistant dict is merged into a default reply)."""
        history = self.chats[chat_id]['chat']['history']
        uid, aid = str(uuid4()), str(uuid4())
        parent = history['currentId']
        history['messages'][uid] = {
            'id': uid,
            'parentId': parent,
            'role': 'user',
            'content': user_text,
            'childrenIds': [aid],
        }
        history['messages'][aid] = {
            'id': aid,
            'parentId': uid,
            'role': 'assistant',
            'content': '',
            'done': True,
            'childrenIds': [],
            **(assistant or {}),
        }
        history['currentId'] = aid
        return uid, aid

    # --- scripted handler -----------------------------------------------------------
    async def handler(self, request, form_data, user=None):
        self.handler_calls.append(form_data)
        chat_id = form_data['chat_id']
        history = self.chats[chat_id]['chat']['history']
        msgs = history['messages']
        user_message = form_data['user_message']
        if user_message['id'] not in msgs:
            msgs[user_message['id']] = {**user_message, 'childrenIds': []}
        aid = form_data['id']
        msgs[aid] = {
            'id': aid,
            'parentId': user_message['id'],
            'role': 'assistant',
            'content': '',
            'done': False,
            'childrenIds': [],
        }
        msgs[user_message['id']].setdefault('childrenIds', []).append(aid)
        history['currentId'] = aid

        outcome = self.script.pop(0) if self.script else {'kind': 'ok'}
        kind = outcome['kind']
        reply = msgs[aid]
        if kind == 'ok':
            reply.update(content=outcome.get('text', 'All done.'), done=True, output=outcome.get('output'))
        elif kind == 'error':
            reply.update(error={'content': outcome['error']}, done=True, content=outcome.get('partial', ''))
        elif kind == 'approval':
            reply.update(
                done=False,
                output=[
                    {
                        'type': 'function_call',
                        'call_id': 'k1',
                        'name': 'local_run_command',
                        'status': 'pending',
                        'arguments': '{}',
                    }
                ],
            )
        return {'status': True, 'task_ids': [], 'chat_id': chat_id}


def build(world: World):
    """Install the fakes and load the real runner. Returns (runner_module, models_module, app)."""
    _db_stub.install()
    for name in ('open_webui', 'open_webui.models', 'open_webui.utils', 'open_webui.socket'):
        _pkg(name)

    models = load('models/chat_queue.py', 'open_webui.models.chat_queue')
    load('utils/usage_limit.py', 'open_webui.utils.usage_limit')

    class Config:
        @staticmethod
        async def get(key, default=None, **_):
            return world.config.get(key, default)

    class Users:
        @staticmethod
        async def get_user_by_id(user_id):
            return world.users.get(user_id)

    class Auths:
        @staticmethod
        async def get_auth_by_id(user_id):
            return SimpleNamespace(active=True, session_stamp='s')

    class ChatForm:
        def __init__(self, chat, folder_id=None, variables=None):
            self.chat, self.folder_id, self.variables = chat, folder_id, variables

    class Chats:
        @staticmethod
        async def get_chat_by_id(chat_id):
            c = world.chats.get(chat_id)
            return SimpleNamespace(**c) if c else None

        @staticmethod
        async def insert_new_chat(chat_id, user_id, form, internal_meta=None, **_):
            world.chats[chat_id] = {'id': chat_id, 'user_id': user_id, 'title': form.chat['title'], 'chat': form.chat}
            return SimpleNamespace(id=chat_id)

        @staticmethod
        async def get_message_by_id_and_message_id(chat_id, message_id):
            c = world.chats.get(chat_id)
            return c['chat']['history']['messages'].get(message_id) if c else None

    async def has_active_tasks(redis, chat_id):
        return chat_id in world.busy_chats

    async def stop_item_tasks(redis, chat_id):
        world.busy_chats.discard(chat_id)

    class Sio:
        async def emit(self, event, payload, room=None):
            data = payload.get('data', {})
            world.events.append((room, data.get('type'), data.get('data')))

    async def notify_target(user_id, body, title='', app_name=''):
        world.webhooks.append((user_id, title, body))

    async def get_all_models(request, user=None):
        return {}

    async def resolve_defaults(app, model_id):
        return {}

    _mod('open_webui.models.config', Config=Config)
    _mod('open_webui.models.users', Users=Users)
    _mod('open_webui.models.auths', Auths=Auths)
    _mod('open_webui.models.chats', Chats=Chats, ChatForm=ChatForm)
    _mod('open_webui.utils.auth', create_token=lambda **kw: 'token')
    _mod('open_webui.utils.misc', parse_duration=lambda v: None)
    _mod('open_webui.utils.models', get_all_models=get_all_models)
    _mod(
        'open_webui.utils.automations',
        _build_request=lambda app, token=None: SimpleNamespace(app=app),
        _resolve_model_defaults=resolve_defaults,
    )
    _mod('open_webui.tasks', has_active_tasks=has_active_tasks, stop_item_tasks=stop_item_tasks)
    _mod('open_webui.socket.main', sio=Sio())
    _mod('open_webui.utils.notifications', notify_target=notify_target)

    runner = load('utils/chat_queue.py', 'open_webui.utils.chat_queue')
    runner.REPLY_POLL_SECONDS = 0.01
    runner.LIMIT_MARGIN = 0

    app = SimpleNamespace(
        state=SimpleNamespace(
            MODELS={'claude': {}}, CHAT_COMPLETION_HANDLER=world.handler, redis=None, WEBUI_NAME='Open WebUI'
        )
    )
    return runner, models, app
