"""REST API of the local agent (see tools/local_agent.py).

The tools themselves are called by the model through the chat pipeline; this router only gives the
UI what it needs: is the agent available to me, which folders, and a way to see/stop the background
processes it started. Administrators can change its settings here.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from open_webui.models.config import Config
from open_webui.tools import local_agent
from open_webui.utils.auth import get_admin_user, get_verified_user
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

router = APIRouter()


def _as_user(user) -> dict:
    return {'id': user.id, 'role': user.role}


async def _require_available(user) -> local_agent.Settings:
    settings = await local_agent.get_settings()
    if not local_agent.user_may_use(_as_user(user), settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='The local agent is not available.')
    return settings


@router.get('/info')
async def get_info(user=Depends(get_verified_user)):
    return await local_agent.public_info(_as_user(user))


class ValidateForm(BaseModel):
    path: str


@router.post('/validate-cwd')
async def validate_cwd(form: ValidateForm, user=Depends(get_verified_user)):
    """Check a working folder typed by the user and return its normalised path."""
    settings = await _require_available(user)
    try:
        return {'path': str(local_agent.resolve_workspace(form.path.strip() or None, settings.roots))}
    except local_agent.LocalAgentError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get('/processes')
async def get_processes(user=Depends(get_verified_user)):
    await _require_available(user)
    return {'processes': local_agent.list_user_processes(user.id)}


@router.delete('/processes/{process_id}')
async def stop_process(process_id: str, user=Depends(get_verified_user)):
    await _require_available(user)
    if not local_agent.stop_user_process(user.id, process_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Process not found.')
    return {'status': True}


# Administration


@router.get('/config')
async def get_config(user=Depends(get_admin_user)):
    settings = await local_agent.get_settings()
    return {
        'enable': settings.enabled,
        'admin_only': settings.admin_only,
        'allowed_roots': [str(r) for r in settings.roots],
        'command_timeout': settings.timeout,
        'max_output': settings.max_output,
        'max_processes': settings.max_processes,
        'running_in_docker': os.path.exists('/.dockerenv'),
    }


class ConfigForm(BaseModel):
    enable: Optional[bool] = None
    admin_only: Optional[bool] = None
    allowed_roots: Optional[list[str]] = Field(default=None, max_length=20)
    command_timeout: Optional[int] = Field(default=None, ge=5, le=local_agent.MAX_TIMEOUT)
    max_output: Optional[int] = Field(default=None, ge=1000, le=500_000)
    max_processes: Optional[int] = Field(default=None, ge=1, le=20)


@router.post('/config')
async def set_config(form: ConfigForm, user=Depends(get_admin_user)):
    updates: dict = {}
    if form.enable is not None:
        updates['local_agent.enable'] = form.enable
    if form.admin_only is not None:
        updates['local_agent.admin_only'] = form.admin_only
    if form.command_timeout is not None:
        updates['local_agent.command_timeout'] = form.command_timeout
    if form.max_output is not None:
        updates['local_agent.max_output'] = form.max_output
    if form.max_processes is not None:
        updates['local_agent.max_processes'] = form.max_processes

    if form.allowed_roots is not None:
        roots = []
        for entry in form.allowed_roots:
            entry = entry.strip()
            if not entry:
                continue
            path = Path(os.path.realpath(os.path.expanduser(entry)))
            if not path.is_dir():
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'Not a folder: {entry}')
            if path.parent == path:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='A whole drive / the filesystem root is too broad: choose a project or home folder.',
                )
            roots.append(str(path))
        updates['local_agent.allowed_roots'] = os.pathsep.join(roots)

    if updates:
        await Config.upsert(updates)
    return await get_config(user)
