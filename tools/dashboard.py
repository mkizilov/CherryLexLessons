import argparse
import os
import shutil
from importlib import metadata
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def port_number(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('Port must be an integer from 1 to 65535.') from error
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError('Port must be an integer from 1 to 65535.')
    return number


def parser():
    result = argparse.ArgumentParser(
        description='Local CherryLex lesson dashboard. Opens paused; no paid work starts automatically.',
        epilog='Run from the project root: .venv/bin/python tools/dashboard.py. '
               'Install local dependencies with .venv/bin/python -m pip install -r requirements-dashboard.txt. '
               'The server binds only 127.0.0.1; no public bind option is supported.',
    )
    result.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1],
                        help='Lesson project root (defaults to this checkout).')
    result.add_argument('--port', type=port_number, default=8091, help='Loopback port (default: 8091).')
    return result


def check_dependencies():
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11+ is required. Use the project .venv/bin/python.')
    for name, minimum in (('nicegui', (3, 17, 1)), ('psutil', (7, 1, 0))):
        try:
            installed = metadata.version(name)
            parts = tuple(int(part) for part in installed.split('.')[:3])
        except (metadata.PackageNotFoundError, ValueError) as error:
            raise ValueError(f'{name} is missing or unsupported. Run .venv/bin/python -m pip install -r requirements-dashboard.txt, then use .venv/bin/python tools/dashboard.py.') from error
        if parts < minimum or (name == 'nicegui' and parts[0] != 3):
            raise ValueError(f'{name} {installed} is unsupported. Install requirements-dashboard.txt in the project .venv.')


def configure_worker_binary():
    """Verify the V2 worker before opening or claiming the persistent queue."""
    from lesson_factory.opencode_worker import verify_binary
    executable = os.environ.get('OPENCODE_BINARY') or shutil.which('opencode')
    if not executable or not Path(executable).is_absolute() or not os.access(executable, os.X_OK):
        raise ValueError('An absolute OpenCode V2 executable is required; set OPENCODE_BINARY.')
    verify_binary(executable)
    os.environ['OPENCODE_BINARY'] = str(Path(executable).resolve())
    return os.environ['OPENCODE_BINARY']


def main(argv=None):
    arguments = parser().parse_args(argv)
    runner = None
    try:
        check_dependencies()
        executable = configure_worker_binary()
        root = arguments.root.resolve()
        if not all((root / name).is_dir() for name in ('lesson_factory', 'reference', 'tools')):
            raise ValueError('--root must be a CherryLexLessons checkout with lesson_factory, reference and tools.')
        from nicegui import ui
        from lesson_factory.dashboard_preview import PreviewService
        from lesson_factory.dashboard_runner import Runner
        from lesson_factory.dashboard_store import Store
        from lesson_factory.dashboard_ui import install_dashboard

        from lesson_factory.worker_preflight import prepare
        guard = prepare(root, executable)
        store = Store(root)
        runner = Runner(root, store, preflight=guard)
        install_dashboard(root, store, runner, PreviewService(root), port=arguments.port)
        ui.run(host='127.0.0.1', port=arguments.port, title='CherryLex · Lesson studio',
               reload=False, show=False, dark=False, fastapi_docs=False, on_air=None,
               endpoint_documentation='none')
        return 0
    except (ValueError, ImportError, RuntimeError, OSError) as error:
        if isinstance(error, ValueError):
            print(str(error), file=sys.stderr)
        else:
            print(f'Dashboard could not start ({type(error).__name__}). Check local dependencies, port availability and whether another dashboard owns the scheduler. No new work was authorized.', file=sys.stderr)
        return 1
    finally:
        if runner is not None:
            runner.close()


if __name__ == '__main__':
    raise SystemExit(main())
