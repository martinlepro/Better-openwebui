"""The real utils/tool_permissions.py with the local agent branch, imports stubbed."""

import json
import sys
import types


from _load import load, stubbed


def _mod(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def _build():
    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            self.status_code = status_code

    packages = {}
    for pkg in ('open_webui', 'open_webui.models', 'open_webui.utils', 'open_webui.tools'):
        module = types.ModuleType(pkg)
        module.__path__ = []
        packages[pkg] = sys.modules.get(pkg, module)
    stubs = {
        **packages,
        'fastapi': _mod('fastapi', HTTPException=HTTPException),
        'open_webui.models.config': _mod(
            'open_webui.models.config', Config=types.SimpleNamespace(get_many=None, default_value=None)
        ),
        'open_webui.utils.json_codec': _mod(
            'open_webui.utils.json_codec',
            JSONCodec=types.SimpleNamespace(dumps=lambda o, **k: json.dumps(o, sort_keys=True, default=str)),
        ),
        'open_webui.utils.misc': _mod(
            'open_webui.utils.misc', add_or_update_system_message=None, get_content_from_message=None
        ),
        'open_webui.utils.tool_calling': _mod('open_webui.utils.tool_calling', strip_tool_approval_prompt=lambda x: x),
        'open_webui.utils.tool_search': _mod(
            'open_webui.utils.tool_search',
            parse_decision_scores=None,
            post_decisions=None,
            validate_decisions_config=None,
        ),
    }
    with stubbed(stubs):  # restored afterwards so other test files keep their own fakes
        load('tools/local_agent.py', 'open_webui.tools.local_agent')
        return load('utils/tool_permissions.py', 'open_webui.utils.tool_permissions')


tp = _build()


def local_tool(name='local_run_command'):
    return {'tool_id': f'builtin:{name}', 'type': 'builtin', 'spec': {'name': name}}


def meta(**params):
    return {'params': params}


async def test_full_cycle_ask_then_approved_then_changed_args():
    tool, args = local_tool(), {'command': 'npm test'}
    call = {'name': 'local_run_command', 'call_id': 'c1'}
    m = meta(tool_approval_mode='full')  # global mode "full" must NOT bypass the local policy

    assert await tp.requires_tool_approval({}, m, call, tool, args) is True  # must ask
    assert call['approval_fingerprint'] and 'approved' not in call

    call['approved'] = True  # user clicked approve
    assert await tp.requires_tool_approval({}, m, call, tool, args) is False  # now it runs

    changed = {'command': 'npm test && rm -rf dist'}  # model changed the command after approval
    assert await tp.requires_tool_approval({}, m, call, tool, changed) is True
    assert 'approved' not in call


async def test_approved_flag_without_matching_fingerprint_is_not_enough():
    call = {'name': 'local_run_command', 'approved': True}  # forged / stale flag
    assert await tp.requires_tool_approval({}, meta(), call, local_tool(), {'command': 'x'}) is True


async def test_policies():
    call = lambda n: {'name': n}
    assert (
        await tp.requires_tool_approval({}, meta(), call('local_read_file'), local_tool('local_read_file'), {}) is False
    )
    assert (
        await tp.requires_tool_approval(
            {}, meta(local_agent_approval='edits'), call('local_edit_file'), local_tool('local_edit_file'), {}
        )
        is False
    )
    assert (
        await tp.requires_tool_approval(
            {}, meta(local_agent_approval='edits'), call('local_run_command'), local_tool(), {'command': 'make'}
        )
        is True
    )
    assert (
        await tp.requires_tool_approval(
            {}, meta(local_agent_approval='auto'), call('local_run_command'), local_tool(), {'command': 'make'}
        )
        is False
    )
    assert (
        await tp.requires_tool_approval({}, meta(), call('local_run_command'), local_tool(), {'command': 'git status'})
        is False
    )


async def test_other_tools_keep_the_original_behaviour():
    other = {'tool_id': 'builtin:search_web', 'type': 'builtin', 'spec': {}}
    assert (
        await tp.requires_tool_approval({}, meta(tool_approval_mode='full'), {'name': 'search_web'}, other, {}) is False
    )
    assert (
        await tp.requires_tool_approval({}, meta(tool_approval_mode='ask'), {'name': 'search_web'}, other, {}) is True
    )
    # a user-defined tool cannot claim the local agent's policy (or escape the global one) by naming itself builtin:local_*
    spoof = {'tool_id': 'builtin:local_run_command', 'type': 'external', 'spec': {}}
    assert await tp.requires_tool_approval({}, meta(tool_approval_mode='ask'), {'name': 'x'}, spoof, {}) is True
