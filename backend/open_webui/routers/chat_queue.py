"""REST API of the chat queue (see utils/chat_queue.py for how items are executed)."""

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from open_webui.constants import ERROR_MESSAGES
from open_webui.models.chat_queue import (
    ACTIVE_STATUSES,
    ChatQueue,
    ChatQueueItemForm,
    ChatQueueItemModel,
    ChatQueueItemUpdateForm,
    DEFAULT_PRIORITY,
    DEFAULT_STATE_SETTINGS,
    FINISHED_STATUSES,
    KIND_MESSAGE,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_PENDING,
    merged_settings,
)
from open_webui.models.chats import Chats
from open_webui.models.config import Config
from open_webui.utils import chat_queue as runner
from open_webui.utils.auth import get_verified_user
from open_webui.utils.usage_limit import detect_limit, error_to_text
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

router = APIRouter()

ALLOWED_FEATURES = {'web_search', 'code_interpreter', 'image_generation', 'memory'}


# Helpers


async def require_queue() -> None:
    if not await Config.get('queue.enable', True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.UNAUTHORIZED)


def state_response(state, user_id: str) -> dict:
    return {
        'paused': state.paused,
        'limit_until': state.limit_until,
        'limit_kind': state.limit_kind,
        'limit_message': state.limit_message,
        'batch_active': state.batch_active,
        'running': user_id in runner._running_users,
        'settings': merged_settings(state),
    }


async def clean_options(options: Optional[dict], user) -> Optional[dict]:
    """Keep only options the queue understands; validate the local agent ones against the admin policy."""
    if not options:
        return None
    clean: dict = {}

    features = options.get('features')
    if isinstance(features, dict):
        clean['features'] = {k: bool(v) for k, v in features.items() if k in ALLOWED_FEATURES}

    if options.get('tool_approval_mode') in ('ask', 'auto', 'full'):
        clean['tool_approval_mode'] = options['tool_approval_mode']

    agent = options.get('local_agent')
    if isinstance(agent, dict) and agent.get('enabled'):
        from open_webui.tools import local_agent

        settings = await local_agent.get_settings()
        if not local_agent.user_may_use({'id': user.id, 'role': user.role}, settings):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='The local agent is not available.')
        entry = {'enabled': True}
        cwd = str(agent.get('cwd') or '').strip()
        if cwd:
            try:
                entry['cwd'] = str(local_agent.resolve_workspace(cwd, settings.roots))
            except local_agent.LocalAgentError as e:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        approval = agent.get('approval')
        entry['approval'] = approval if approval in local_agent.APPROVAL_POLICIES else local_agent.DEFAULT_POLICY
        clean['local_agent'] = entry

    return clean or None


async def get_own_item(item_id: str, user) -> ChatQueueItemModel:
    item = await ChatQueue.get_for_user(user.id, item_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return item


async def get_own_chat(chat_id: str, user):
    chat = await Chats.get_chat_by_id(chat_id)
    if not chat or chat.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return chat


# Read


@router.get('/')
async def get_queue(user=Depends(get_verified_user)):
    await require_queue()
    items = await ChatQueue.list_by_user(user.id)
    state = await ChatQueue.get_state(user.id)
    return {
        'items': [item.model_dump() for item in items],
        'state': state_response(state, user.id),
        'now': int(time.time()),
    }


# Items


@router.post('/items')
async def create_item(form: ChatQueueItemForm, user=Depends(get_verified_user)):
    await require_queue()
    # Resume items are created by the system (or through /resume-chat), never directly.
    form.kind = KIND_MESSAGE
    form.prompt = form.prompt.strip()
    if not form.prompt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='The message is empty.')

    limit = int(await Config.get('queue.max_items', 200) or 200)
    counts = await ChatQueue.count_by_status(user.id)
    if sum(counts.get(s, 0) for s in ACTIVE_STATUSES) >= limit:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'The queue is full ({limit} items).')

    if form.chat_id:
        chat = await get_own_chat(form.chat_id, user)
        form.title = form.title or chat.title
    elif not form.model_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Choose a model for the new conversation.')

    form.title = (form.title or '').strip() or runner.make_title(form.prompt)
    form.options = await clean_options(form.options, user)
    item = await ChatQueue.insert(user.id, form)
    await runner.refresh_ui(user.id)
    return item


class ReorderForm(BaseModel):
    ids: list[str] = Field(max_length=1000)


@router.post('/items/reorder')
async def reorder_items(form: ReorderForm, user=Depends(get_verified_user)):
    await require_queue()
    await ChatQueue.reorder(user.id, form.ids)
    await runner.refresh_ui(user.id)
    return {'status': True}


@router.post('/clear-finished')
async def clear_finished(user=Depends(get_verified_user)):
    await require_queue()
    removed = await ChatQueue.delete_finished(user.id)
    await runner.refresh_ui(user.id)
    return {'removed': removed}


@router.patch('/items/{item_id}')
async def update_item(item_id: str, form: ChatQueueItemUpdateForm, user=Depends(get_verified_user)):
    await require_queue()
    item = await get_own_item(item_id, user)
    fields: dict = {}

    if form.status is not None:
        target = form.status
        if target == STATUS_CANCELLED:
            if item.status in FINISHED_STATUSES:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='This item is already finished.')
            fields.update(status=STATUS_CANCELLED, finished_at=int(time.time()))
        elif target == STATUS_PAUSED and item.status == STATUS_PENDING:
            fields['status'] = STATUS_PAUSED
        elif target == STATUS_PENDING and item.status in (STATUS_PAUSED, STATUS_FAILED, STATUS_CANCELLED):
            fields.update(status=STATUS_PENDING, last_error=None, finished_at=None, next_attempt_at=None)
            if item.status != STATUS_PAUSED:
                fields['attempts'] = 0
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f'Cannot change a {item.status} item to {target}.'
            )

    editing = any(v is not None for v in (form.prompt, form.title, form.model_id, form.options))
    if editing:
        if item.status not in (STATUS_PENDING, STATUS_PAUSED, STATUS_FAILED, STATUS_CANCELLED):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='This item can no longer be edited.')
        if form.prompt is not None:
            fields['prompt'] = form.prompt.strip()
        if form.title is not None:
            fields['title'] = form.title.strip() or None
        if form.model_id is not None:
            fields['model_id'] = form.model_id or None
        if form.options is not None:
            new_options = await clean_options(form.options, user) or {}
            # keep the runtime bookkeeping (ids of the message already sent) across edits
            runtime = (item.options or {}).get('runtime')
            fields['options'] = {**new_options, **({'runtime': runtime} if runtime else {})} or None
    if form.priority is not None:
        fields['priority'] = form.priority

    if fields:
        item = await ChatQueue.update_by_id(item_id, fields)
        await runner.refresh_ui(user.id)
    return item


@router.delete('/items/{item_id}')
async def delete_item(item_id: str, user=Depends(get_verified_user)):
    await require_queue()
    await get_own_item(item_id, user)
    await ChatQueue.delete_by_id(user.id, item_id)  # a running item notices and stops its reply
    await runner.refresh_ui(user.id)
    return {'status': True}


@router.post('/items/{item_id}/run-now')
async def run_item_now(item_id: str, user=Depends(get_verified_user)):
    """Move an item to the front of the line."""
    await require_queue()
    item = await get_own_item(item_id, user)
    if item.status not in (STATUS_PENDING, STATUS_PAUSED, STATUS_FAILED, STATUS_CANCELLED):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='This item cannot be moved.')
    waiting = await ChatQueue.list_by_user(user.id, statuses=[STATUS_PENDING, STATUS_PAUSED])
    front = min((i.priority for i in waiting), default=DEFAULT_PRIORITY) - 1
    fields = {'priority': front, 'status': STATUS_PENDING, 'next_attempt_at': None}
    if item.status in (STATUS_FAILED, STATUS_CANCELLED):
        fields.update(attempts=0, last_error=None, finished_at=None)
    item = await ChatQueue.update_by_id(item_id, fields)
    await runner.refresh_ui(user.id)
    return item


# Queue state


@router.post('/pause')
async def pause_queue(user=Depends(get_verified_user)):
    await require_queue()
    state = await ChatQueue.set_state(user.id, paused=True)
    await runner.refresh_ui(user.id)
    return state_response(state, user.id)


@router.post('/resume')
async def resume_queue(user=Depends(get_verified_user)):
    await require_queue()
    state = await ChatQueue.set_state(user.id, paused=False)
    await runner.refresh_ui(user.id)
    return state_response(state, user.id)


@router.post('/limit/clear')
async def clear_limit(request: Request, user=Depends(get_verified_user)):
    """The user knows the limit is over (or wants to try now): stop waiting."""
    await require_queue()
    await runner.clear_limit(request.app, user.id)
    return state_response(await ChatQueue.get_state(user.id), user.id)


@router.put('/settings')
async def update_settings(form: dict, user=Depends(get_verified_user)):
    await require_queue()
    clean: dict = {}
    for key, default in DEFAULT_STATE_SETTINGS.items():
        if key not in form:
            continue
        value = form[key]
        if isinstance(default, bool):
            clean[key] = bool(value)
        elif key == 'resume_message':
            text = str(value or '').strip()
            clean[key] = text[:2000] or default
    state = await ChatQueue.get_state(user.id)
    state = await ChatQueue.set_state(user.id, settings={**(state.settings or {}), **clean})
    return state_response(state, user.id)


# From a chat: "resume automatically when the limit is over"


class ResumeChatForm(BaseModel):
    chat_id: str
    message_id: Optional[str] = None


@router.post('/resume-chat')
async def resume_chat(request: Request, form: ResumeChatForm, user=Depends(get_verified_user)):
    """Arm an automatic resume for a conversation that stopped on a limit.

    Reads the failed reply to work out when the limit ends, pauses the queue until then and
    queues a resume that runs first. Safe to press twice: one resume per chat.
    """
    await require_queue()
    chat = await get_own_chat(form.chat_id, user)
    history = (chat.chat or {}).get('history') or {}
    message_id = form.message_id or history.get('currentId')
    message = (history.get('messages') or {}).get(message_id) if message_id else None

    info = None
    if message and message.get('error'):
        info = detect_limit(error_to_text(message['error']))
        if info is not None:
            await runner.register_limit(request.app, user.id, info)

    item = await runner.enqueue_resume(user.id, form.chat_id, title=chat.title, trigger=info)
    state = await ChatQueue.get_state(user.id)
    return {
        'item': item,
        'limit': info.to_dict() if info else None,
        'state': state_response(state, user.id),
    }
