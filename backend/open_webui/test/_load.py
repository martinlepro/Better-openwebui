"""Load a pure-Python Open WebUI module by file path.

`import open_webui` pulls in typer/uvicorn and the whole app. The modules tested
here have no such dependency, so they are loaded straight from their file.
A file is executed once; asking for it again (under another name) returns the
same module, which keeps SQLAlchemy tables from being declared twice.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_loaded: dict[Path, object] = {}


def load(relative_path: str, name: str | None = None):
    path = (ROOT / relative_path).resolve()
    name = name or path.stem
    module = _loaded.get(path)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _loaded[path] = module
    sys.modules[name] = module
    return module


from contextlib import contextmanager  # noqa: E402

_MISSING = object()


@contextmanager
def stubbed(modules: dict):
    """Temporarily put fake modules in sys.modules; the previous ones come back on exit."""
    previous = {name: sys.modules.get(name, _MISSING) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, old in previous.items():
            if old is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
