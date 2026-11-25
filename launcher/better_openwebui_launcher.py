"""Better Open WebUI launcher.

A small program (compiled to BetterOpenWebUI.exe / a Linux binary by PyInstaller in CI) that
installs and starts Better Open WebUI on your computer:

  1. downloads the `uv` tool (pinned version, SHA-256 checked) the first time,
  2. lets uv create an isolated Python 3.11 environment and install the app from the
     ``open_webui-*.whl`` that sits next to the launcher (built by the CI),
  3. starts the server on http://127.0.0.1:8080 and opens your browser.

Your data (chats, settings, the secret key) lives in a per-user folder and survives updates.
The first start downloads the dependencies (several GB: PyTorch and friends), later starts are fast.

Examples:
    BetterOpenWebUI                          start (installs on first run)
    BetterOpenWebUI --agent C:\\Users\\me\\projets   enable the local agent on this folder
    BetterOpenWebUI --lan                    reachable from your phone on the same Wi-Fi
    BetterOpenWebUI --reinstall              reinstall the app from the wheel
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from typing import Optional

LAUNCHER_VERSION = '1.0.0'
APP_DIR_NAME = 'BetterOpenWebUI'
PACKAGE_NAME = 'open-webui'
UV_VERSION = '0.12.10'  # same version as the project's CI
PYTHON_VERSION = '3.11'
DEFAULT_PORT = 8080
HEALTH_TIMEOUT_SECONDS = 20 * 60  # first start runs migrations and may take a while

MESSAGES = {
    'fr': {
        'home': 'Dossier de l’application : {path}',
        'uv_download': 'Téléchargement de uv {version}…',
        'uv_bad_checksum': 'La somme de contrôle de uv ne correspond pas : téléchargement refusé.',
        'no_wheel': (
            'Aucune wheel open_webui-*.whl trouvée à côté du lanceur.\n'
            'Télécharge l’archive complète (lanceur + wheel) depuis la page Releases/Actions du dépôt,\n'
            'ou indique la wheel avec --wheel CHEMIN.'
        ),
        'installing': 'Installation de Better Open WebUI (le premier lancement télécharge plusieurs Go)…',
        'up_to_date': 'Installation à jour.',
        'starting': 'Démarrage sur {url} …',
        'ready': 'Prêt : {url}   (Ctrl+C pour arrêter)',
        'agent_on': 'Agent local ACTIVÉ sur : {path}',
        'lan_warning': 'Accessible depuis le réseau local : crée ton compte administrateur AVANT de partager l’adresse.',
        'stopping': 'Arrêt…',
        'server_died': 'Le serveur s’est arrêté (code {code}). Consulte les messages ci-dessus.',
        'timeout': 'Le serveur n’a pas répondu après {minutes} minutes.',
        'bad_agent': 'Le dossier de l’agent n’existe pas : {path}',
        'unsupported': 'Plateforme non prise en charge : {system} {machine}',
        'command_failed': 'La commande a échoué : {cmd}',
    },
    'en': {
        'home': 'Application folder: {path}',
        'uv_download': 'Downloading uv {version}…',
        'uv_bad_checksum': 'uv checksum mismatch: download refused.',
        'no_wheel': (
            'No open_webui-*.whl found next to the launcher.\n'
            'Download the full archive (launcher + wheel) from the repository Releases/Actions page,\n'
            'or pass the wheel with --wheel PATH.'
        ),
        'installing': 'Installing Better Open WebUI (the first run downloads several GB)…',
        'up_to_date': 'Installation is up to date.',
        'starting': 'Starting on {url} …',
        'ready': 'Ready: {url}   (Ctrl+C to stop)',
        'agent_on': 'Local agent ENABLED on: {path}',
        'lan_warning': 'Reachable from the local network: create your admin account BEFORE sharing the address.',
        'stopping': 'Stopping…',
        'server_died': 'The server stopped (code {code}). See the messages above.',
        'timeout': 'The server did not answer after {minutes} minutes.',
        'bad_agent': 'The agent folder does not exist: {path}',
        'unsupported': 'Unsupported platform: {system} {machine}',
        'command_failed': 'Command failed: {cmd}',
    },
}


class LauncherError(Exception):
    """A problem the user can read and act on (printed without a traceback)."""


def language() -> str:
    for name in ('BOW_LANG', 'LC_ALL', 'LC_MESSAGES', 'LANG'):
        value = (os.environ.get(name) or '').lower()
        if value:
            return 'fr' if value.startswith('fr') else 'en'
    try:
        import locale

        code = (locale.getlocale()[0] or '').lower()
    except Exception:
        code = ''
    return 'fr' if code.startswith('fr') else 'en'


def say(key: str, **kwargs) -> str:
    return MESSAGES[language()][key].format(**kwargs)


def log(key: str, **kwargs) -> None:
    print(say(key, **kwargs), flush=True)


# --------------------------------------------------------------------------------------
# Paths / platform
# --------------------------------------------------------------------------------------


def app_home(override: Optional[str] = None) -> Path:
    """Per-user folder holding the environment, the data and the secret key."""
    explicit = override or os.environ.get('BOW_HOME')
    if explicit:
        return Path(explicit).expanduser().resolve()
    system = platform.system()
    if system == 'Windows':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
        return base / APP_DIR_NAME
    if system == 'Darwin':
        return Path.home() / 'Library' / 'Application Support' / APP_DIR_NAME
    base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share')
    return base / 'better-openwebui'


def launcher_dir() -> Path:
    """Folder of the executable (or of this script when not frozen)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_UV_TRIPLES = {
    ('Linux', 'x86_64'): 'x86_64-unknown-linux-gnu',
    ('Linux', 'aarch64'): 'aarch64-unknown-linux-gnu',
    ('Windows', 'amd64'): 'x86_64-pc-windows-msvc',
    ('Windows', 'arm64'): 'aarch64-pc-windows-msvc',
    ('Darwin', 'x86_64'): 'x86_64-apple-darwin',
    ('Darwin', 'arm64'): 'aarch64-apple-darwin',
}


def uv_triple(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    if machine in ('x86_64', 'amd64'):
        machine = 'amd64' if system == 'Windows' else 'x86_64'
    if machine == 'aarch64' and system == 'Darwin':
        machine = 'arm64'
    try:
        return _UV_TRIPLES[(system, machine)]
    except KeyError:
        raise LauncherError(say('unsupported', system=system, machine=machine)) from None


def uv_asset_url(triple: str, version: str = UV_VERSION) -> str:
    extension = 'zip' if 'windows' in triple else 'tar.gz'
    return f'https://github.com/astral-sh/uv/releases/download/{version}/uv-{triple}.{extension}'


def venv_python(venv: Path) -> Path:
    return venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def venv_script(venv: Path, name: str) -> Path:
    return venv / (f'Scripts/{name}.exe' if os.name == 'nt' else f'bin/{name}')


# --------------------------------------------------------------------------------------
# uv
# --------------------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def parse_checksum(text: str) -> str:
    """'<hash>  filename' -> '<hash>' (also accepts a bare hash)."""
    token = text.strip().split()[0] if text.strip() else ''
    if len(token) != 64 or any(c not in '0123456789abcdefABCDEF' for c in token):
        raise LauncherError(say('uv_bad_checksum'))
    return token.lower()


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={'User-Agent': f'{APP_DIR_NAME}/{LAUNCHER_VERSION}'})
    with urllib.request.urlopen(request, timeout=60) as response, open(destination, 'wb') as out:
        shutil.copyfileobj(response, out)


def _extract_uv(archive: Path, destination: Path) -> None:
    wanted = 'uv.exe' if os.name == 'nt' else 'uv'
    destination.parent.mkdir(parents=True, exist_ok=True)
    if archive.suffix == '.zip':
        with zipfile.ZipFile(archive) as bundle:
            member = next((n for n in bundle.namelist() if Path(n).name == wanted), None)
            if member is None:
                raise LauncherError(say('uv_bad_checksum'))
            with bundle.open(member) as source, open(destination, 'wb') as out:
                shutil.copyfileobj(source, out)
    else:
        with tarfile.open(archive) as bundle:
            member = next((m for m in bundle.getmembers() if m.isfile() and Path(m.name).name == wanted), None)
            if member is None:
                raise LauncherError(say('uv_bad_checksum'))
            source = bundle.extractfile(member)
            assert source is not None
            with source, open(destination, 'wb') as out:
                shutil.copyfileobj(source, out)
    destination.chmod(0o755)


def ensure_uv(home: Path) -> Path:
    """Return a usable uv binary, downloading the pinned release the first time."""
    override = os.environ.get('BOW_UV')
    if override:
        return Path(override)
    target = home / 'bin' / ('uv.exe' if os.name == 'nt' else 'uv')
    if target.exists():
        return target

    triple = uv_triple()
    url = uv_asset_url(triple)
    log('uv_download', version=UV_VERSION)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / url.rsplit('/', 1)[1]
        try:
            _download(url, archive)
            expected = parse_checksum(urllib.request.urlopen(url + '.sha256', timeout=60).read().decode())
        except (urllib.error.URLError, OSError) as e:
            raise LauncherError(f'{url}: {e}') from e
        if sha256_file(archive) != expected:
            raise LauncherError(say('uv_bad_checksum'))
        _extract_uv(archive, target)
    return target


def uv_env(home: Path) -> dict:
    env = dict(os.environ)
    env.update(
        UV_PYTHON_INSTALL_DIR=str(home / 'python'),
        UV_CACHE_DIR=str(home / 'cache'),
        UV_LINK_MODE='copy',
    )
    if not sys.stdout.isatty():
        env['UV_NO_PROGRESS'] = '1'
    return env


def run(command: list, env: dict, cwd: Optional[Path] = None) -> None:
    result = subprocess.run([str(c) for c in command], env=env, cwd=str(cwd) if cwd else None)
    if result.returncode != 0:
        raise LauncherError(say('command_failed', cmd=' '.join(str(c) for c in command)))


# --------------------------------------------------------------------------------------
# Installation
# --------------------------------------------------------------------------------------


def find_wheel(explicit: Optional[str]) -> Path:
    candidates = []
    if explicit or os.environ.get('BOW_WHEEL'):
        candidates.append(Path(explicit or os.environ['BOW_WHEEL']).expanduser())
    else:
        for folder in (launcher_dir(), Path.cwd()):
            found = sorted(folder.glob('open_webui-*.whl'))
            if found:
                candidates.append(found[-1])
                break
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise LauncherError(say('no_wheel'))


def read_marker(home: Path) -> dict:
    try:
        return json.loads((home / 'installed.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write_marker(home: Path, data: dict) -> None:
    (home / 'installed.json').write_text(json.dumps(data, indent=2), encoding='utf-8')


def ensure_installed(home: Path, wheel: Path, *, force: bool = False) -> Path:
    """Create the environment and install ``wheel`` when needed. Returns the venv path."""
    venv = home / 'venv'
    fingerprint = sha256_file(wheel)
    marker = read_marker(home)
    healthy = venv_script(venv, 'open-webui').exists() and venv_python(venv).exists()
    if healthy and not force and marker.get('wheel_sha256') == fingerprint:
        log('up_to_date')
        return venv

    log('installing')
    uv = ensure_uv(home)
    env = uv_env(home)
    if not venv_python(venv).exists():
        shutil.rmtree(venv, ignore_errors=True)  # a half-created environment would make `uv venv` refuse
        run([uv, 'venv', '--python', PYTHON_VERSION, venv], env)
    # --reinstall-package: a rebuilt wheel keeps the same version number, uv would skip it otherwise.
    run([uv, 'pip', 'install', '--python', venv_python(venv), '--reinstall-package', PACKAGE_NAME, wheel], env)
    write_marker(home, {'wheel': wheel.name, 'wheel_sha256': fingerprint, 'launcher': LAUNCHER_VERSION})
    return venv


# --------------------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------------------


def port_is_free(port: int, host: str = '127.0.0.1') -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) != 0


def pick_port(preferred: int, host: str = '127.0.0.1') -> int:
    for port in range(preferred, preferred + 50):
        if port_is_free(port, host):
            return port
    return preferred


def server_environment(home: Path, agent: Optional[Path]) -> dict:
    env = dict(os.environ)
    env.setdefault('DATA_DIR', str(home / 'data'))
    env.setdefault('SCARF_NO_ANALYTICS', 'true')
    env.setdefault('DO_NOT_TRACK', 'true')
    env.setdefault('ANONYMIZED_TELEMETRY', 'false')
    if agent is not None:
        env['ENABLE_LOCAL_AGENT'] = 'true'
        env['LOCAL_AGENT_ALLOWED_ROOTS'] = str(agent)
    return env


def wait_until_healthy(url: str, process: subprocess.Popen, timeout: float = HEALTH_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise LauncherError(say('server_died', code=code))
        try:
            with urllib.request.urlopen(url + '/health', timeout=3) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(1)
    raise LauncherError(say('timeout', minutes=int(timeout // 60)))


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == 'nt':
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=15)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        process.kill()
        process.wait(timeout=10)


def _raise_keyboard_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def install_stop_signals() -> None:
    """Make Ctrl+C, a closed terminal (SIGHUP) and `kill` (SIGTERM) all stop the server cleanly.

    Without this, killing the launcher would leave the server running in the background.
    SIGINT is set explicitly because a launcher started in the background inherits it as ignored.
    """
    for name in ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK'):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, _raise_keyboard_interrupt)
        except (ValueError, OSError):  # not the main thread / unsupported here
            pass


def serve(venv: Path, home: Path, *, host: str, port: int, agent: Optional[Path], open_browser: bool) -> int:
    url = f'http://127.0.0.1:{port}'
    command = [str(venv_script(venv, 'open-webui')), 'serve', '--host', host, '--port', str(port)]
    log('starting', url=url)
    install_stop_signals()
    kwargs: dict = {}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(command, cwd=str(home), env=server_environment(home, agent), **kwargs)
    try:
        wait_until_healthy(url, process)
        log('ready', url=url)
        if open_browser:
            webbrowser.open(url)
        return process.wait()
    except KeyboardInterrupt:
        log('stopping')
        return 0
    finally:
        stop_process(process)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_DIR_NAME, description='Install and start Better Open WebUI.')
    parser.add_argument(
        '--port', type=int, default=DEFAULT_PORT, help=f'port (default {DEFAULT_PORT}; next free one if busy)'
    )
    parser.add_argument('--lan', action='store_true', help='listen on the local network (phone, other PC)')
    parser.add_argument('--no-browser', action='store_true', help='do not open the browser')
    parser.add_argument('--agent', metavar='FOLDER', help='enable the local agent, restricted to this folder')
    parser.add_argument('--wheel', metavar='FILE', help='path of the open_webui wheel (default: next to the launcher)')
    parser.add_argument('--home', metavar='FOLDER', help='where to store the environment and data')
    parser.add_argument('--reinstall', action='store_true', help='reinstall the application from the wheel')
    parser.add_argument('--version', action='store_true', help='print the launcher version and exit')
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f'{APP_DIR_NAME} launcher {LAUNCHER_VERSION}')
        return 0
    try:
        home = app_home(args.home)
        home.mkdir(parents=True, exist_ok=True)
        log('home', path=home)

        agent = None
        if args.agent:
            agent = Path(args.agent).expanduser().resolve()
            if not agent.is_dir():
                raise LauncherError(say('bad_agent', path=agent))
            log('agent_on', path=agent)

        wheel = find_wheel(args.wheel)
        venv = ensure_installed(home, wheel, force=args.reinstall)

        host = '0.0.0.0' if args.lan else '127.0.0.1'
        if args.lan:
            log('lan_warning')
        port = pick_port(args.port)
        return serve(venv, home, host=host, port=port, agent=agent, open_browser=not args.no_browser)
    except LauncherError as e:
        print(f'\n{e}', file=sys.stderr)
        return 1


def entry() -> int:
    code = main()
    # Double-clicked on Windows: keep the console open so the error can be read.
    if code != 0 and getattr(sys, 'frozen', False) and os.name == 'nt' and sys.stdin and sys.stdin.isatty():
        try:
            input('\nEntrée pour fermer / Press Enter to close…')
        except EOFError:
            pass
    return code


if __name__ == '__main__':
    sys.exit(entry())
