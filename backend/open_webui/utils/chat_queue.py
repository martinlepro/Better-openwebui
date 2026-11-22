"""Chat queue runner.

Sends queued messages/conversations automatically, one at a time per user, highest
priority first. When the model provider reports a usage/rate limit the queue pauses
until the limit resets, then resumes by itself. Notifies the user when:

  * a limit is reached          (``limit_reached``)
  * one conversation is done    (``item_done``)  - with a preview and the files it changed
  * everything is done          (``queue_done``)
  * an item needs approval      (``needs_approval``) / failed (``item_failed``)

Replies are produced by the normal chat pipeline (same code path as the chat UI and
automations), so filters, tools, knowledge, memory and saving to the chat all behave
exactly as in the browser - the browser just doesn't need to stay open.

Environment:
    CHAT_QUEUE_POLL_INTERVAL   seconds between queue polls (default 5)
    CHAT_QUEUE_LIMIT_MARGIN    seconds added after a limit's announced reset (default 20)
    CHAT_QUEUE_MAX_ATTEMPTS    attempts per item before it is marked failed (default 500)
    CHAT_QUEUE_LANG            language of webhook texts: fr | en (default fr)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from open_webui.models.chat_queue import (
    ChatQueue,
    ChatQueueItemForm,
    ChatQueueItemModel,
    DEFAULT_RESUME_MESSAGE,
    KIND_RESUME,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NEEDS_APPROVAL,
    STATUS_PAUSED,
    STATUS_PENDING,
    STATUS_RUNNING,
    merged_settings,
)
from open_webui.utils.usage_limit import (
    LimitInfo,
    default_wait_seconds,
    detect_limit,
    error_to_text,
    format_wait,
)

log = logging.getLogger(__name__)

POLL_INTERVAL = max(1, int(os.getenv('CHAT_QUEUE_POLL_INTERVAL', '5')))
LIMIT_MARGIN = max(0, int(os.getenv('CHAT_QUEUE_LIMIT_MARGIN', '20')))
MAX_ATTEMPTS = max(1, int(os.getenv('CHAT_QUEUE_MAX_ATTEMPTS', '500')))  # long limits are probed every <=30 min
WEBHOOK_LANG = os.getenv('CHAT_QUEUE_LANG', 'fr').lower()
REPLY_POLL_SECONDS = 2.0
BUSY_RETRY_SECONDS = 30  # chat has a live reply: try again shortly

# Tools whose calls mean "a file changed" (see tools/local_agent.py).
FILE_WRITING_TOOLS = {'local_write_file', 'local_edit_file'}

_running_users: set[str] = set()
_limit_registry: dict[str, tuple[float, LimitInfo]] = {}  # assistant message id -> info


# --------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------


def _truncate(text: str, size: int) -> str:
    text = (text or '').strip()
    return text if len(text) <= size else text[: size - 1].rstrip() + '…'


def make_title(prompt: str) -> str:
    first_line = (prompt or '').strip().splitlines()[0] if (prompt or '').strip() else ''
    return _truncate(first_line, 60) or 'Queued chat'


def reply_text(reply: Optional[dict]) -> str:
    """Plain text of an assistant message (content, or the text items of its output)."""
    if not reply:
        return ''
    if reply.get('content'):
        return str(reply['content'])
    parts = []
    for item in reply.get('output') or []:
        if isinstance(item, dict) and item.get('type') == 'message':
            for part in item.get('content') or []:
                if isinstance(part, dict) and part.get('text'):
                    parts.append(str(part['text']))
    return '\n'.join(parts)


def changed_files(reply: Optional[dict]) -> list[str]:
    """Paths written or edited by the local agent tools during this reply."""
    import json

    paths: list[str] = []
    for item in (reply or {}).get('output') or []:
        if not isinstance(item, dict) or item.get('type') != 'function_call':
            continue
        if item.get('name') not in FILE_WRITING_TOOLS or item.get('status') not in (None, 'completed'):
            continue
        try:
            arguments = item.get('arguments')
            arguments = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        except Exception:
            continue
        path = arguments.get('path')
        if isinstance(path, str) and path not in paths:
            paths.append(path)
    return paths


def has_pending_tool_calls(reply: Optional[dict]) -> bool:
    done_ids = {
        i.get('call_id')
        for i in (reply or {}).get('output') or []
        if isinstance(i, dict) and i.get('type') == 'function_call_output'
    }
    return any(
        isinstance(i, dict)
        and i.get('type') == 'function_call'
        and (i.get('call_id') or i.get('id')) not in done_ids
        and i.get('status') in {'pending', 'queued', 'requires_approval'}
        for i in (reply or {}).get('output') or []
    )


def plan_send(history: Optional[dict], item: ChatQueueItemModel, resume_message: str = DEFAULT_RESUME_MESSAGE) -> dict:
    """Decide what to send for ``item`` given the current chat history.

    Returns one of:
      {'mode': 'append', 'parent_id': <id|None>, 'text': str}   add a user message
      {'mode': 'retry',  'user_message': dict}                  regenerate the reply to an existing user message
      {'mode': 'skip',   'reason': str}                         nothing to do (counts as done)
    """
    messages = (history or {}).get('messages') or {}
    leaf_id = (history or {}).get('currentId')
    leaf = messages.get(leaf_id) if leaf_id else None
    runtime = (item.options or {}).get('runtime') or {}

    # Re-attempt of an item that already sent its message but whose reply failed (limit...).
    anchor_id = runtime.get('assistant_message_id')
    anchor = messages.get(anchor_id) if anchor_id else None
    if anchor is None and item.kind == KIND_RESUME:
        anchor = leaf if leaf and leaf.get('role') == 'assistant' else None
        if anchor is None:
            return {'mode': 'skip', 'reason': 'nothing to resume'}

    if anchor is not None:
        if not anchor.get('error'):
            if anchor.get('done', True):
                return {'mode': 'skip', 'reason': 'already answered'}
            return {'mode': 'skip', 'reason': 'reply still running'}
        parent = messages.get(anchor.get('parentId'))
        if not (anchor.get('content') or anchor.get('output')) and parent and parent.get('role') == 'user':
            return {'mode': 'retry', 'user_message': parent}
        text = (item.prompt if item.kind == KIND_RESUME else None) or resume_message
        return {'mode': 'append', 'parent_id': anchor.get('id') or anchor_id, 'text': text}

    return {'mode': 'append', 'parent_id': leaf_id if leaf else None, 'text': item.prompt}


# --------------------------------------------------------------------------------------
# Texts for webhooks (the browser composes its own localized texts from the same data)
# --------------------------------------------------------------------------------------

_TEXTS = {
    'fr': {
        'limit_reached': ('Limite atteinte', 'Limite atteinte. Reprise automatique à {at} (dans {wait}). {pending} en attente.'),
        'item_done': ('Conversation terminée', '{title}\n{preview}'),
        'queue_done': ('File d’attente terminée', '{done} terminée(s), {failed} en échec.'),
        'needs_approval': ('Approbation requise', '« {title} » attend votre validation pour exécuter une action.'),
        'item_failed': ('Échec d’une conversation', '« {title} » a échoué : {error}'),
    },
    'en': {
        'limit_reached': ('Limit reached', 'Limit reached. Resuming automatically at {at} (in {wait}). {pending} waiting.'),
        'item_done': ('Conversation finished', '{title}\n{preview}'),
        'queue_done': ('Queue finished', '{done} finished, {failed} failed.'),
        'needs_approval': ('Approval required', '"{title}" needs your approval to run an action.'),
        'item_failed': ('Conversation failed', '"{title}" failed: {error}'),
    },
}


def render_text(event: str, data: dict, lang: Optional[str] = None) -> tuple[str, str]:
    table = _TEXTS.get((lang or WEBHOOK_LANG), _TEXTS['en'])
    title, body = table.get(event, (event, ''))
    fields = {
        'title': data.get('title') or '',
        'preview': data.get('preview') or '',
        'pending': data.get('pending', 0),
        'done': data.get('done', 0),
        'failed': data.get('failed', 0),
        'error': _truncate(str(data.get('error') or ''), 200),
        'wait': format_wait(data.get('wait_seconds') or 0),
        'at': _format_clock(data.get('reset_at')),
    }
    return title, body.format(**fields).strip()


def _format_clock(timestamp: Optional[float]) -> str:
    if not timestamp:
        return '?'
    moment = datetime.fromtimestamp(timestamp)
    today = datetime.now().date()
    return moment.strftime('%H:%M') if moment.date() == today else moment.strftime('%d/%m %H:%M')


# --------------------------------------------------------------------------------------
# Events & notifications
# --------------------------------------------------------------------------------------


async def emit_queue_event(user_id: str, event_type: str, data: dict) -> None:
    """Push a ``queue:*`` event to every open tab of the user."""
    try:
        from open_webui.socket.main import sio

        await sio.emit('events', {'data': {'type': event_type, 'data': data}}, room=f'user:{user_id}')
    except Exception:
        log.debug('queue event emit failed', exc_info=True)


async def notify_user(app: Any, user_id: str, event: str, **data) -> None:
    """Notify about ``event`` if the user enabled it: browser (toast/Notification/sound) + webhook."""
    state = await ChatQueue.get_state(user_id)
    if not merged_settings(state).get(f'notify_{event}', True):
        return
    payload = {'event': event, 'timestamp': int(time.time()), **data}
    await emit_queue_event(user_id, f'queue:{event}', payload)

    try:
        from open_webui.models.config import Config

        if await Config.get('ui.enable_user_webhooks'):
            from open_webui.utils.notifications import notify_target

            title, body = render_text(event, payload)
            await notify_target(
                user_id, body, title=title, app_name=getattr(getattr(app, 'state', None), 'WEBUI_NAME', 'Open WebUI')
            )
    except Exception:
        # No target configured / webhook down: the in-app notification already went out.
        log.debug('queue webhook notification skipped', exc_info=True)


async def refresh_ui(user_id: str) -> None:
    await emit_queue_event(user_id, 'queue:update', {})


# --------------------------------------------------------------------------------------
# Limit handling
# --------------------------------------------------------------------------------------


def _remember_limit(message_id: Optional[str], info: LimitInfo) -> None:
    if not message_id:
        return
    now = time.time()
    for key in [k for k, (ts, _) in _limit_registry.items() if now - ts > 600]:
        _limit_registry.pop(key, None)
    _limit_registry[message_id] = (now, info)


def _pop_limit(message_id: Optional[str]) -> Optional[LimitInfo]:
    entry = _limit_registry.pop(message_id, None) if message_id else None
    return entry[1] if entry else None


async def register_limit(app: Any, user_id: str, info: LimitInfo, attempt: int = 0) -> int:
    """Record that the provider limit is active for this user; notify the first time.

    Returns the epoch second at which the queue may run again.
    """
    now = int(time.time())
    exact = info.reset_at is not None
    if exact:
        until = int(info.reset_at) + LIMIT_MARGIN
    else:
        until = now + int(default_wait_seconds(info.kind, attempt))

    state = await ChatQueue.get_state(user_id)
    already = bool(state.limit_until and state.limit_until > now)
    if already:
        if exact and not state.limit_exact:
            pass  # a real reset time from the provider beats an earlier guess
        elif state.limit_exact and not exact:
            until, exact = state.limit_until, True  # never replace a real time with a guess
        else:
            until = max(until, state.limit_until)  # same quality: the later one rules
            exact = exact and state.limit_exact

    await ChatQueue.set_state(
        user_id,
        limit_until=until,
        limit_kind=info.kind,
        limit_message=_truncate(info.message, 500),
        limit_exact=exact,
        limit_notified=True,
    )
    if not already or not state.limit_notified:
        counts = await ChatQueue.count_by_status(user_id)
        await notify_user(
            app,
            user_id,
            'limit_reached',
            kind=info.kind,
            reset_at=until,
            wait_seconds=max(0, until - now),
            known_reset=exact,
            pending=counts.get(STATUS_PENDING, 0) + counts.get(STATUS_RUNNING, 0),
            message=_truncate(info.message, 300),
        )
    await refresh_ui(user_id)
    return until


async def clear_limit(app: Any, user_id: str, *, silent: bool = False) -> None:
    await ChatQueue.set_state(
        user_id, limit_until=None, limit_kind=None, limit_message=None, limit_exact=False, limit_notified=False
    )
    if not silent:
        await emit_queue_event(user_id, 'queue:limit_cleared', {})
    await refresh_ui(user_id)


async def enqueue_resume(
    user_id: str, chat_id: str, *, title: Optional[str] = None, trigger: Optional[LimitInfo] = None
) -> Optional[ChatQueueItemModel]:
    """Queue a resume of ``chat_id`` (idempotent: one active resume per chat)."""
    existing = await ChatQueue.list_by_user(user_id, statuses=[STATUS_PENDING, STATUS_RUNNING, STATUS_PAUSED])
    for item in existing:
        if item.chat_id == chat_id and item.kind == KIND_RESUME:
            return item
    settings = merged_settings(await ChatQueue.get_state(user_id))
    options = {'trigger': trigger.to_dict()} if trigger else {}
    item = await ChatQueue.insert(
        user_id,
        ChatQueueItemForm(
            prompt=settings.get('resume_message') or DEFAULT_RESUME_MESSAGE,
            title=title,
            chat_id=chat_id,
            kind=KIND_RESUME,
            options=options,
        ),
    )
    await refresh_ui(user_id)
    return item


async def on_chat_error(
    request: Any,
    user: Any,
    metadata: dict,
    error: Any,
    *,
    status: Optional[int] = None,
    headers: Optional[dict] = None,
) -> None:
    """Middleware hook: a chat reply failed.

    Remembers limit details for the queue runner, pauses the queue until the limit
    resets and - when the user turned auto-resume on - queues a resume of this chat.
    Never raises: a failure here must not affect the chat itself.
    """
    try:
        from open_webui.models.config import Config

        if not await Config.get('queue.enable', True):
            return
        info = detect_limit(error, status=status, headers=dict(headers) if headers else None)
        if info is None:
            return
        _remember_limit(metadata.get('message_id'), info)

        chat_id = metadata.get('chat_id')
        session_id = str(metadata.get('session_id') or '')
        # Queue runs handle their own limit; internal/automation runs are not interactive chats.
        if session_id.startswith('queue:') or metadata.get('internal') or metadata.get('automation_id'):
            return

        app = request.app
        await register_limit(app, user.id, info)
        settings = merged_settings(await ChatQueue.get_state(user.id))
        if settings.get('auto_resume') and chat_id and not str(chat_id).startswith(('local:', 'channel:')):
            from open_webui.models.chats import Chats

            chat = await Chats.get_chat_by_id(chat_id)
            if chat and chat.user_id == user.id:
                await enqueue_resume(user.id, chat_id, title=chat.title, trigger=info)
    except Exception:
        log.exception('chat queue limit hook failed')


# --------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------


async def _build_context(app: Any, user: Any):
    from open_webui.models.auths import Auths
    from open_webui.models.config import Config
    from open_webui.utils.auth import create_token
    from open_webui.utils.automations import _build_request
    from open_webui.utils.misc import parse_duration
    from open_webui.utils.models import get_all_models
    from datetime import timedelta

    auth = await Auths.get_auth_by_id(user.id)
    if auth is None or not auth.active:
        raise ValueError('Queue owner is no longer active')
    try:
        expires = parse_duration(str(await Config.get('queue.auth_token_expires_in', '12h')))
    except ValueError:
        expires = None
    token = create_token(
        data={'id': user.id, 'typ': 'automation', 'session_stamp': auth.session_stamp},
        expires_delta=expires or timedelta(hours=12),
    )
    if not app.state.MODELS:
        await get_all_models(_build_request(app, token=token), user=user)
    return _build_request(app, token=token)


async def _chat_is_busy(app: Any, chat_id: str) -> bool:
    try:
        from open_webui.tasks import has_active_tasks

        return await has_active_tasks(app.state.redis, chat_id)
    except Exception:
        return False


async def _prepare_chat(user: Any, item: ChatQueueItemModel, model_id: str):
    """Return (chat_id, history) creating an empty chat for new conversations."""
    from open_webui.models.chats import ChatForm, Chats

    if item.chat_id:
        chat = await Chats.get_chat_by_id(item.chat_id)
        if not chat or chat.user_id != user.id:
            raise ValueError('The conversation no longer exists')
        return chat.id, (chat.chat or {}).get('history') or {}, chat

    chat_id = str(uuid4())
    chat = await Chats.insert_new_chat(
        chat_id,
        user.id,
        ChatForm(
            chat={
                'id': chat_id,
                'title': item.title or make_title(item.prompt),
                'models': [model_id],
                'history': {'messages': {}, 'currentId': None},
                'messages': [],
                'tags': [],
                'files': [],
                'timestamp': int(time.time() * 1000),
            }
        ),
        internal_meta={'queue_item_id': item.id},
    )
    if not chat:
        raise ValueError('Failed to create the conversation')
    await ChatQueue.update_by_id(item.id, {'chat_id': chat_id})
    return chat_id, {'messages': {}, 'currentId': None}, chat


def _request_options(item: ChatQueueItemModel) -> dict:
    """form_data additions from the item's options (features, params, approval mode...)."""
    options = item.options or {}
    extra: dict = {}
    features = options.get('features')
    if isinstance(features, dict) and features:
        extra['features'] = features
    params = dict(options.get('params') or {})
    if options.get('tool_approval_mode') in ('ask', 'auto', 'full'):
        params['tool_approval_mode'] = options['tool_approval_mode']
    local_agent = options.get('local_agent') or {}
    if local_agent.get('enabled'):
        extra.setdefault('features', {})['local_agent'] = True
        if local_agent.get('cwd'):
            params['local_agent_cwd'] = local_agent['cwd']
        if local_agent.get('approval') in ('ask', 'edits', 'auto'):
            params['local_agent_approval'] = local_agent['approval']
    if params:
        extra['params'] = params
    return extra


async def _send(app: Any, user: Any, item: ChatQueueItemModel) -> dict:
    """Send the item's message. Returns {'status': 'sent', chat_id, assistant_id, ...} or {'status': 'skip'|'busy'}."""
    from open_webui.models.config import Config
    from open_webui.utils.automations import _resolve_model_defaults

    settings = merged_settings(await ChatQueue.get_state(user.id))
    request = await _build_context(app, user)

    chat = None
    model_id = item.model_id
    if item.chat_id:
        from open_webui.models.chats import Chats

        chat = await Chats.get_chat_by_id(item.chat_id)
        if not chat or chat.user_id != user.id:
            raise ValueError('The conversation no longer exists')
        model_id = model_id or next(iter((chat.chat or {}).get('models') or []), None)
    if not model_id:
        raise ValueError('No model selected for this queued conversation')

    chat_id, history, _ = await _prepare_chat(user, item, model_id)
    if await _chat_is_busy(app, chat_id):
        return {'status': 'busy'}

    plan = plan_send(history, item, settings.get('resume_message') or DEFAULT_RESUME_MESSAGE)
    if plan['mode'] == 'skip':
        return {'status': 'skip', 'reason': plan['reason'], 'chat_id': chat_id}

    assistant_id = str(uuid4())
    if plan['mode'] == 'retry':
        user_message = dict(plan['user_message'])
        parent_id = user_message.get('parentId')
    else:
        parent_id = plan['parent_id']
        user_message = {
            'id': str(uuid4()),
            'parentId': parent_id,
            'role': 'user',
            'content': plan['text'],
            'timestamp': int(time.time()),
            'models': [model_id],
        }

    form_data = {
        **await _resolve_model_defaults(app, model_id),
        'model': model_id,
        'messages': [{'role': 'user', 'content': user_message.get('content', '')}],
        'stream': True,
        'chat_id': chat_id,
        'id': assistant_id,
        'parent_id': parent_id,
        'user_message': user_message,
        'session_id': f'queue:{item.id}',
        'background_tasks': {},
    }
    extra = _request_options(item)
    # Merge (don't overwrite) model defaults like default features.
    if 'features' in extra:
        form_data['features'] = {**form_data.get('features', {}), **extra['features']}
    if 'params' in extra:
        form_data['params'] = {**form_data.get('params', {}), **extra['params']}

    ui_settings = (user.settings.ui or {}) if user.settings else {}
    memory = ui_settings.get('memory')
    if memory is None:
        defaults = await Config.get('ui.default_interface_settings') or {}
        memory = defaults.get('memory')
    if memory is None:
        memory = await Config.get('memories.enable')
    if memory:
        form_data.setdefault('features', {})['memory'] = True

    runtime = {'user_message_id': user_message['id'], 'assistant_message_id': assistant_id}
    await ChatQueue.update_by_id(
        item.id,
        {'options': {**(item.options or {}), 'runtime': runtime}, 'result_message_id': assistant_id},
    )

    result = await app.state.CHAT_COMPLETION_HANDLER(request, form_data, user=user)
    if isinstance(result, dict) and result.get('queued'):
        return {'status': 'busy'}
    return {'status': 'sent', 'chat_id': chat_id, 'assistant_id': assistant_id}


async def _wait_for_reply(app: Any, item_id: str, chat_id: str, assistant_id: str) -> Optional[dict]:
    """Poll until the reply is finished (or needs approval). Returns the message, None if cancelled."""
    from open_webui.models.chats import Chats

    started = time.monotonic()
    idle_since: Optional[float] = None
    while True:
        await asyncio.sleep(REPLY_POLL_SECONDS)

        current = await ChatQueue.get_by_id(item_id)
        if current is None or current.status == STATUS_CANCELLED:
            try:
                from open_webui.tasks import stop_item_tasks

                await stop_item_tasks(app.state.redis, chat_id)
            except Exception:
                log.debug('could not stop tasks for cancelled queue item', exc_info=True)
            return None

        reply = await Chats.get_message_by_id_and_message_id(chat_id, assistant_id)
        active = await _chat_is_busy(app, chat_id)
        if reply is None:
            if time.monotonic() - started > 60:
                return {'error': {'content': 'The reply was never created'}, 'done': True}
            continue
        if active:
            idle_since = None
            continue
        # No task is running any more: the reply is final once saved.
        idle_since = idle_since or time.monotonic()
        if reply.get('error') or reply.get('done') or has_pending_tool_calls(reply):
            return reply
        if time.monotonic() - idle_since > 10:  # stopped without a final state
            return reply


async def _finalize(app: Any, user_id: str, item: ChatQueueItemModel, chat_id: str, reply: dict) -> None:
    """Turn a finished reply into the item's final state + notifications."""
    now = int(time.time())
    title = item.title or make_title(item.prompt)
    assistant_id = reply.get('id') or item.result_message_id

    if reply.get('error'):
        error = error_to_text(reply.get('error'))
        info = _pop_limit(assistant_id) or detect_limit(error)
        if info is not None and item.attempts < MAX_ATTEMPTS:
            until = await register_limit(app, user_id, info, attempt=item.attempts - 1)
            await ChatQueue.update_by_id(
                item.id,
                {'status': STATUS_PENDING, 'last_error': _truncate(error, 1000), 'next_attempt_at': None},
            )
            log.info('Queue item %s waits for limit until %s', item.id, until)
        else:
            await ChatQueue.update_by_id(
                item.id,
                {'status': STATUS_FAILED, 'last_error': _truncate(error, 1000), 'finished_at': now},
            )
            await notify_user(app, user_id, 'item_failed', item_id=item.id, chat_id=chat_id, title=title, error=error)
        return

    if has_pending_tool_calls(reply) and not reply.get('done'):
        await ChatQueue.update_by_id(item.id, {'status': STATUS_NEEDS_APPROVAL, 'last_error': None})
        await notify_user(app, user_id, 'needs_approval', item_id=item.id, chat_id=chat_id, title=title)
        return

    if not reply.get('done'):
        # No task is running, yet the reply never reached its final state.
        message = 'The reply stopped before it finished'
        await ChatQueue.update_by_id(item.id, {'status': STATUS_FAILED, 'last_error': message, 'finished_at': now})
        await notify_user(app, user_id, 'item_failed', item_id=item.id, chat_id=chat_id, title=title, error=message)
        return

    await ChatQueue.update_by_id(item.id, {'status': STATUS_DONE, 'finished_at': now, 'last_error': None})
    await notify_user(
        app,
        user_id,
        'item_done',
        item_id=item.id,
        chat_id=chat_id,
        title=title,
        preview=_truncate(reply_text(reply), 280),
        files=changed_files(reply),
    )


async def _after_item(app: Any, user_id: str) -> None:
    """Fire ``queue_done`` once nothing is left to run."""
    counts = await ChatQueue.count_by_status(user_id)
    remaining = sum(counts.get(s, 0) for s in (STATUS_PENDING, STATUS_RUNNING, STATUS_NEEDS_APPROVAL))
    state = await ChatQueue.get_state(user_id)
    if remaining == 0 and state.batch_active:
        since = state.batch_started_at or 0
        items = await ChatQueue.list_by_user(user_id, statuses=[STATUS_DONE, STATUS_FAILED])
        batch = [i for i in items if (i.finished_at or 0) >= since]
        await ChatQueue.set_state(user_id, batch_active=False, batch_started_at=None)
        await notify_user(
            app,
            user_id,
            'queue_done',
            done=sum(1 for i in batch if i.status == STATUS_DONE),
            failed=sum(1 for i in batch if i.status == STATUS_FAILED),
        )
    await refresh_ui(user_id)


async def run_item(app: Any, item: ChatQueueItemModel) -> None:
    """Execute one claimed item end to end. Never raises."""
    from open_webui.models.users import Users

    user_id = item.user_id
    try:
        user = await Users.get_user_by_id(user_id)
        if not user or user.role not in ('user', 'admin'):
            raise ValueError('Queue owner is no longer allowed to run')
        await refresh_ui(user_id)

        sent = await _send(app, user, item)
        if sent['status'] == 'busy':
            # Someone is talking in that chat right now: don't interleave, retry shortly.
            await ChatQueue.update_by_id(
                item.id,
                {
                    'status': STATUS_PENDING,
                    'next_attempt_at': int(time.time()) + BUSY_RETRY_SECONDS,
                    'attempts': max(0, item.attempts - 1),
                },
            )
            return
        if sent['status'] == 'skip':
            await ChatQueue.update_by_id(
                item.id,
                {'status': STATUS_DONE, 'finished_at': int(time.time()), 'last_error': sent.get('reason')},
            )
            return

        reply = await _wait_for_reply(app, item.id, sent['chat_id'], sent['assistant_id'])
        if reply is None:  # cancelled while running
            return
        reply = {**reply, 'id': reply.get('id') or sent['assistant_id']}
        fresh = await ChatQueue.get_by_id(item.id) or item
        await _finalize(app, user_id, fresh, sent['chat_id'], reply)
    except asyncio.CancelledError:
        await ChatQueue.update_by_id(item.id, {'status': STATUS_PENDING})
        raise
    except Exception as e:
        log.exception('Queue item %s failed', item.id)
        await ChatQueue.update_by_id(
            item.id, {'status': STATUS_FAILED, 'last_error': _truncate(str(e), 1000), 'finished_at': int(time.time())}
        )
        await notify_user(
            app,
            user_id,
            'item_failed',
            item_id=item.id,
            chat_id=item.chat_id,
            title=item.title or make_title(item.prompt),
            error=str(e),
        )
    finally:
        # Bookkeeping first, release second: the user's next item must not start while we
        # are still deciding whether the queue is finished.
        try:
            await _after_item(app, user_id)
        except Exception:
            log.exception('queue after-item bookkeeping failed')
        finally:
            _running_users.discard(user_id)


# --------------------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------------------


async def _check_approvals(app: Any) -> None:
    """Items waiting for tool approval finish when the user answers (in the chat UI)."""
    from open_webui.models.chats import Chats

    for item in await ChatQueue.list_needing_approval():
        chat_id, message_id = item.chat_id, item.result_message_id
        if not chat_id or not message_id:
            await ChatQueue.update_by_id(item.id, {'status': STATUS_FAILED, 'finished_at': int(time.time())})
            continue
        reply = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
        if reply is None or await _chat_is_busy(app, chat_id):
            continue
        if reply.get('error') or (reply.get('done') and not has_pending_tool_calls(reply)):
            await _finalize(app, item.user_id, item, chat_id, {**reply, 'id': message_id})
            await _after_item(app, item.user_id)


async def _tick(app: Any) -> None:
    from open_webui.models.config import Config

    if not await Config.get('queue.enable', True):
        return
    now = int(time.time())
    await _check_approvals(app)

    for user_id in await ChatQueue.user_ids_with_pending(now):
        if user_id in _running_users:
            continue
        state = await ChatQueue.get_state(user_id)
        if state.paused:
            continue
        if state.limit_until:
            if state.limit_until > now:
                continue
            await clear_limit(app, user_id)  # the limit is over: carry on
        counts = await ChatQueue.count_by_status(user_id)
        if counts.get(STATUS_RUNNING, 0) > 0:
            continue  # another instance is running something for this user
        item = await ChatQueue.claim_next(user_id, now)
        if not item:
            continue
        if not state.batch_active:
            await ChatQueue.set_state(user_id, batch_active=True, batch_started_at=now)
        _running_users.add(user_id)
        asyncio.create_task(run_item(app, item))


async def chat_queue_worker_loop(app: Any) -> None:
    """Background loop started with the application (see main.py lifespan)."""
    log.info('Chat queue worker started (poll interval: %ss)', POLL_INTERVAL)
    try:
        interrupted = await ChatQueue.reset_interrupted()
        if interrupted:
            log.info('Chat queue: %s interrupted item(s) put back in line', interrupted)
    except Exception:
        log.exception('Chat queue startup cleanup failed')

    while True:
        try:
            await _tick(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Chat queue worker error')
        await asyncio.sleep(POLL_INTERVAL)
