import importlib.util
import io
import socket
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    'launcher', Path(__file__).resolve().parents[1] / 'better_openwebui_launcher.py'
)
L = importlib.util.module_from_spec(SPEC)
sys.modules['launcher'] = L
SPEC.loader.exec_module(L)


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setenv('BOW_LANG', 'en')
    for name in ('BOW_HOME', 'BOW_UV', 'BOW_WHEEL'):
        monkeypatch.delenv(name, raising=False)


# ---- platform ------------------------------------------------------------------------


@pytest.mark.parametrize(
    'system,machine,triple',
    [
        ('Linux', 'x86_64', 'x86_64-unknown-linux-gnu'),
        ('Linux', 'aarch64', 'aarch64-unknown-linux-gnu'),
        ('Windows', 'AMD64', 'x86_64-pc-windows-msvc'),
        ('Windows', 'ARM64', 'aarch64-pc-windows-msvc'),
        ('Darwin', 'x86_64', 'x86_64-apple-darwin'),
        ('Darwin', 'arm64', 'aarch64-apple-darwin'),
    ],
)
def test_uv_triple(system, machine, triple):
    assert L.uv_triple(system, machine) == triple


def test_uv_triple_unsupported():
    with pytest.raises(L.LauncherError, match='Unsupported'):
        L.uv_triple('Linux', 'riscv64')


def test_uv_asset_url():
    assert L.uv_asset_url('x86_64-pc-windows-msvc', '1.2.3').endswith('/1.2.3/uv-x86_64-pc-windows-msvc.zip')
    assert L.uv_asset_url('x86_64-unknown-linux-gnu', '1.2.3').endswith('/1.2.3/uv-x86_64-unknown-linux-gnu.tar.gz')
    assert L.uv_asset_url('aarch64-apple-darwin').startswith('https://github.com/astral-sh/uv/releases/download/')


def test_app_home_per_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(L.platform, 'system', lambda: 'Windows')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert L.app_home() == tmp_path / 'BetterOpenWebUI'
    monkeypatch.setattr(L.platform, 'system', lambda: 'Linux')
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'xdg'))
    assert L.app_home() == tmp_path / 'xdg' / 'better-openwebui'
    monkeypatch.setattr(L.platform, 'system', lambda: 'Darwin')
    assert L.app_home().parts[-2:] == ('Application Support', 'BetterOpenWebUI')


def test_app_home_override(monkeypatch, tmp_path):
    assert L.app_home(str(tmp_path / 'x')) == (tmp_path / 'x').resolve()
    monkeypatch.setenv('BOW_HOME', str(tmp_path / 'env'))
    assert L.app_home() == (tmp_path / 'env').resolve()


def test_language(monkeypatch):
    monkeypatch.setenv('BOW_LANG', 'fr_FR')
    assert L.language() == 'fr'
    assert 'Dossier' in L.say('home', path='x')
    monkeypatch.setenv('BOW_LANG', 'de')
    assert L.language() == 'en'


def test_every_message_exists_in_both_languages():
    assert set(L.MESSAGES['fr']) == set(L.MESSAGES['en'])


# ---- checksum / extraction -------------------------------------------------------------


def test_parse_checksum():
    good = 'a' * 64
    assert L.parse_checksum(f'{good}  uv-x86_64-unknown-linux-gnu.tar.gz\n') == good
    assert L.parse_checksum(good.upper()) == good
    for bad in ('', 'xyz', 'a' * 63, 'g' * 64):
        with pytest.raises(L.LauncherError):
            L.parse_checksum(bad)


def test_sha256_file(tmp_path):
    f = tmp_path / 'f'
    f.write_bytes(b'hello')
    assert L.sha256_file(f) == '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'


def test_extract_uv_from_tar_and_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(L.os, 'name', 'posix')
    tar_path = tmp_path / 'uv.tar.gz'
    with tarfile.open(tar_path, 'w:gz') as t:
        for name, payload in (('uv-x/uvx', b'UVX'), ('uv-x/uv', b'UV-BINARY')):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            t.addfile(info, io.BytesIO(payload))
    out = tmp_path / 'bin' / 'uv'
    L._extract_uv(tar_path, out)
    assert out.read_bytes() == b'UV-BINARY'

    zip_path = tmp_path / 'uv.zip'
    with zipfile.ZipFile(zip_path, 'w') as z:
        z.writestr('uvx', b'UVX')
        z.writestr('uv', b'FROM-ZIP')
    out2 = tmp_path / 'bin2' / 'uv'
    L._extract_uv(zip_path, out2)
    assert out2.read_bytes() == b'FROM-ZIP'


def test_extract_uv_missing_member(tmp_path, monkeypatch):
    monkeypatch.setattr(L.os, 'name', 'posix')
    z = tmp_path / 'bad.zip'
    with zipfile.ZipFile(z, 'w') as zf:
        zf.writestr('readme.txt', b'x')
    with pytest.raises(L.LauncherError):
        L._extract_uv(z, tmp_path / 'o' / 'uv')


def test_ensure_uv_prefers_override_and_existing(tmp_path, monkeypatch):
    monkeypatch.setenv('BOW_UV', '/opt/uv')
    assert L.ensure_uv(tmp_path) == Path('/opt/uv')
    monkeypatch.delenv('BOW_UV')
    existing = tmp_path / 'bin' / ('uv.exe' if L.os.name == 'nt' else 'uv')
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b'x')
    assert L.ensure_uv(tmp_path) == existing  # no download when already there


# ---- wheel lookup -----------------------------------------------------------------------


def test_find_wheel(tmp_path, monkeypatch):
    wheel = tmp_path / 'open_webui-0.13.4-py3-none-any.whl'
    wheel.write_bytes(b'w')
    assert L.find_wheel(str(wheel)) == wheel.resolve()
    monkeypatch.setattr(L, 'launcher_dir', lambda: tmp_path)
    assert L.find_wheel(None) == wheel.resolve()
    monkeypatch.setenv('BOW_WHEEL', str(wheel))
    assert L.find_wheel(None) == wheel.resolve()


def test_find_wheel_picks_latest_and_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(L, 'launcher_dir', lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(L.LauncherError, match='No open_webui'):
        L.find_wheel(None)
    (tmp_path / 'open_webui-0.1.0-py3-none-any.whl').write_bytes(b'a')
    (tmp_path / 'open_webui-0.2.0-py3-none-any.whl').write_bytes(b'b')
    assert L.find_wheel(None).name == 'open_webui-0.2.0-py3-none-any.whl'
    with pytest.raises(L.LauncherError):
        L.find_wheel(str(tmp_path / 'missing.whl'))


# ---- installation logic (uv stubbed) -------------------------------------------------------


@pytest.fixture
def fake_uv(monkeypatch):
    calls = []

    def run(command, env, cwd=None):
        command = [str(c) for c in command]
        calls.append(command)
        if command[1] == 'venv':
            venv = Path(command[-1])
            (venv / 'bin').mkdir(parents=True, exist_ok=True)
            (venv / 'Scripts').mkdir(parents=True, exist_ok=True)
            for name in ('python', 'python.exe', 'open-webui', 'open-webui.exe'):
                (venv / 'bin' / name).write_bytes(b'')
                (venv / 'Scripts' / name).write_bytes(b'')

    monkeypatch.setattr(L, 'run', run)
    monkeypatch.setattr(L, 'ensure_uv', lambda home: Path('uv'))
    return calls


def test_install_is_idempotent_and_reinstalls_rebuilt_wheels(tmp_path, fake_uv):
    home = tmp_path / 'home'
    home.mkdir()
    wheel = tmp_path / 'open_webui-0.13.4-py3-none-any.whl'
    wheel.write_bytes(b'build-1')

    venv = L.ensure_installed(home, wheel)
    assert [c[1] for c in fake_uv] == ['venv', 'pip']
    install = fake_uv[1]
    assert '--reinstall-package' in install and 'open-webui' in install and str(wheel) in install
    assert '3.11' in fake_uv[0]
    assert (home / 'installed.json').exists()

    fake_uv.clear()
    assert L.ensure_installed(home, wheel) == venv
    assert fake_uv == []  # same wheel: nothing to do

    wheel.write_bytes(b'build-2')  # same version number, new content
    L.ensure_installed(home, wheel)
    assert [c[1] for c in fake_uv] == ['pip']  # environment kept, only the app reinstalled

    fake_uv.clear()
    L.ensure_installed(home, wheel, force=True)
    assert [c[1] for c in fake_uv] == ['pip']


def test_broken_environment_is_rebuilt(tmp_path, fake_uv):
    home = tmp_path / 'home'
    (home / 'venv').mkdir(parents=True)
    (home / 'venv' / 'leftover').write_text('x')
    wheel = tmp_path / 'open_webui-1-py3-none-any.whl'
    wheel.write_bytes(b'x')
    L.ensure_installed(home, wheel)
    assert not (home / 'venv' / 'leftover').exists()
    assert fake_uv[0][1] == 'venv'


# ---- server -------------------------------------------------------------------------------


def test_server_environment(tmp_path, monkeypatch):
    monkeypatch.delenv('DATA_DIR', raising=False)
    env = L.server_environment(tmp_path, None)
    assert env['DATA_DIR'] == str(tmp_path / 'data')
    assert 'ENABLE_LOCAL_AGENT' not in env or env['ENABLE_LOCAL_AGENT'] != 'true'
    agent = tmp_path / 'proj'
    env = L.server_environment(tmp_path, agent)
    assert env['ENABLE_LOCAL_AGENT'] == 'true' and env['LOCAL_AGENT_ALLOWED_ROOTS'] == str(agent)
    monkeypatch.setenv('DATA_DIR', '/custom')
    assert L.server_environment(tmp_path, None)['DATA_DIR'] == '/custom'  # user choice wins


def test_pick_port_skips_busy_ports():
    with socket.socket() as busy:
        busy.bind(('127.0.0.1', 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert not L.port_is_free(taken)
        assert L.pick_port(taken) != taken


def test_wait_until_healthy_reports_a_dead_server():
    class Dead:
        def poll(self):
            return 3

    with pytest.raises(L.LauncherError, match='code 3'):
        L.wait_until_healthy('http://127.0.0.1:9', Dead(), timeout=5)


def test_wait_until_healthy_times_out():
    class Alive:
        def poll(self):
            return None

    with pytest.raises(L.LauncherError, match='did not answer'):
        L.wait_until_healthy('http://127.0.0.1:9', Alive(), timeout=0.2)


# ---- CLI ----------------------------------------------------------------------------------


def test_cli_version(capsys):
    assert L.main(['--version']) == 0
    assert L.LAUNCHER_VERSION in capsys.readouterr().out


def test_cli_rejects_unknown_agent_folder(tmp_path, capsys):
    code = L.main(['--home', str(tmp_path / 'h'), '--agent', str(tmp_path / 'nope')])
    assert code == 1
    assert 'does not exist' in capsys.readouterr().err


def test_cli_missing_wheel_is_a_clean_error(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(L, 'launcher_dir', lambda: tmp_path / 'empty')
    monkeypatch.chdir(tmp_path)
    assert L.main(['--home', str(tmp_path / 'h')]) == 1
    assert 'No open_webui' in capsys.readouterr().err


def test_lan_and_agent_flags_reach_the_server(tmp_path, monkeypatch):
    wheel = tmp_path / 'open_webui-1-py3-none-any.whl'
    wheel.write_bytes(b'x')
    seen = {}
    monkeypatch.setattr(L, 'ensure_installed', lambda home, w, force=False: tmp_path / 'venv')

    def fake_serve(venv, home, *, host, port, agent, open_browser):
        seen.update(host=host, port=port, agent=agent, open_browser=open_browser)
        return 0

    monkeypatch.setattr(L, 'serve', fake_serve)
    folder = tmp_path / 'proj'
    folder.mkdir()
    assert L.main(['--home', str(tmp_path / 'h'), '--wheel', str(wheel), '--agent', str(folder), '--no-browser']) == 0
    assert seen['host'] == '127.0.0.1'  # local only by default: the agent runs code
    assert seen['agent'] == folder.resolve() and seen['open_browser'] is False
    assert L.main(['--home', str(tmp_path / 'h'), '--wheel', str(wheel), '--lan']) == 0
    assert seen['host'] == '0.0.0.0'


def test_stop_signals_raise_keyboard_interrupt():
    import signal

    saved = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # what a background launch inherits
        L.install_stop_signals()
        for number in (signal.SIGINT, signal.SIGTERM):
            handler = signal.getsignal(number)
            assert callable(handler)
            with pytest.raises(KeyboardInterrupt):
                handler(number, None)
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)
