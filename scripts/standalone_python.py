#!/usr/bin/env python3
"""Package reviewed local Python modules for an isolated interpreter."""

import argparse
from pathlib import Path
import sys

from bounded_file import read_bounded_text


SCRIPTS = Path(__file__).resolve().parent
SOURCE_LIMIT = 128 * 1024


def with_modules(source, modules):
    """Embed dependencies without consulting the target host's Python search path."""
    return (
        "import sys as _platform_sys\n"
        "from types import ModuleType as _PlatformModule\n"
        f"for _name, _source in {tuple(modules.items())!a}:\n"
        "    _module = _PlatformModule(_name)\n"
        "    _platform_sys.modules[_name] = _module\n"
        "    exec(compile(_source, _name + '.py', 'exec'), _module.__dict__)\n"
        f"exec(compile({source!a}, globals().get('__file__', '<standalone>'), 'exec'))\n"
    )


def standalone_script(path, *dependencies):
    source = read_bounded_text(path, encoding="utf-8", max_bytes=SOURCE_LIMIT)
    modules = {
        name: read_bounded_text(SCRIPTS / f"{name}.py", encoding="utf-8", max_bytes=SOURCE_LIMIT)
        for name in dependencies
    }
    return with_modules(source, modules)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("program", choices=["render_argocd_offline_images.py"])
    args = parser.parse_args()
    sys.stdout.write(standalone_script(SCRIPTS / args.program, "strict_json", "strict_yaml"))


if __name__ == "__main__":
    main()
