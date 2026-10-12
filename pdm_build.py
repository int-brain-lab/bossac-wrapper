"""pdm-backend build hook that bundles the bossac binary with the wheel."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from platform import libc_ver, machine, system
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pdm.backend.hooks import Context  # Only needed for the type annotation

ROOT = Path(__file__).parent.resolve()
REPO_URL = 'https://github.com/shumatech/BOSSA'
BOSSAC_NAME = 'bossac.exe' if system() == 'Windows' else 'bossac'
STAMP_NAME = 'bossac.build'
"""Next to the binary, records what it was built from - see _build_stamp()."""
MACOS_DEPLOYMENT_TARGET = '11.0'


def _read_constant(name: str) -> str:
    content = ROOT.joinpath('bossac', '__init__.py').read_text()
    match = re.search(rf"{name} = ['\"]([^'\"]+)['\"]", content)
    if match is None:
        raise RuntimeError(f'Could not find {name} in bossac/__init__.py')
    return match.group(1)


def _bossac_version() -> str:
    return _read_constant('_BOSSAC_VERSION')


def _bossac_commit() -> str:
    return _read_constant('_BOSSAC_COMMIT')


def _check_version(binary: Path, version: str) -> None:
    # bossac has no --version option - its help text names the version, exit status 1.
    # The version is matched like bossac.version() does, which can't be imported here.
    result = subprocess.run(
        [str(binary), '--help'], capture_output=True, text=True, timeout=30, check=False
    )
    match = re.search(r'\bVersion (\S+)', result.stdout)
    if match is None or match.group(1) != version:
        raise RuntimeError(
            f'{binary} reports unexpected version: {result.stdout.strip()!r} '
            f'(expected {version!r})'
        )


def _make_args(version: str) -> list[str]:
    """Return the variables passed to BOSSA's Makefile for building bossac."""
    # COMMON_CXXFLAGS & co. can't be overridden on the command line without losing the
    # Makefile's own additions to them - CXXFLAGS and LDFLAGS are appended instead
    cxxflags = ['-Wno-error']  # BOSSA 1.9.1 predates the warnings of recent compilers
    ldflags = []
    args = [f'VERSION={version}']
    if system() == 'Linux':
        ldflags += ['-static-libstdc++', '-static-libgcc']
    elif system() == 'Darwin':
        # the Makefile hardcodes '-arch x86_64 -mmacosx-version-min=10.9'
        flags = ['-arch', machine(), f'-mmacosx-version-min={MACOS_DEPLOYMENT_TARGET}']
        cxxflags += flags
        ldflags += flags
    elif system() == 'Windows':
        # the Makefile's Windows rules expect MSYS2's MINGW32 environment - MINGW64
        # works, too. termcap is only needed by bossash.
        args += ['OS=MINGW32', 'COMMON_LIBS=-Wl,--as-needed -lsetupapi']
    args += [f'CXXFLAGS={" ".join(cxxflags)}', f'LDFLAGS={" ".join(ldflags)}']
    return args


def build_bossac(output_dir: Path) -> Path:
    """Clone BOSSA at the pinned version and build bossac into `output_dir`."""
    version = _bossac_version()
    print(f'Building bossac {version} for {system()} {machine()}...')

    # check if git and make executables are available
    for executable in ('git', 'make'):
        if shutil.which(executable) is None:
            raise RuntimeError(f'Could not find {executable} executable')

    output_dir = output_dir.resolve()

    with TemporaryDirectory() as tmp:
        # clone BOSSA - its release tags are the plain version numbers
        src = Path(tmp, 'BOSSA')
        clone = ['git', 'clone', '--depth=1', f'--branch={version}', REPO_URL, str(src)]
        try:
            subprocess.check_call(clone, cwd=tmp)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f'Could not clone BOSSA {version}') from e

        # make sure the tag still points to the pinned commit
        commit = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=src, text=True
        ).strip()
        if commit != (expected := _bossac_commit()):
            raise RuntimeError(
                f'BOSSA {version} is commit {commit}, but {expected} is pinned'
            )

        # build and strip bossac - only its target, so wxWidgets isn't needed
        try:
            subprocess.check_call(
                ['make', 'strip-bossac', *_make_args(version)], cwd=src
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f'Could not build bossac {version}') from e
        built = src / 'bin' / BOSSAC_NAME
        if not built.is_file():
            raise FileNotFoundError('Could not find built bossac binary')

        output_dir.mkdir(parents=True, exist_ok=True)
        binary = Path(shutil.copy2(built, output_dir / built.name))
        if system() != 'Windows':
            binary.chmod(0o755)

    # check & return binary
    _check_version(binary, version)
    output_dir.joinpath(STAMP_NAME).write_text(_build_stamp(version))
    return binary


def _build_stamp(version: str) -> str:
    """Return what a binary is built from - a change in it calls for a rebuild."""
    return json.dumps({'commit': _bossac_commit(), 'make': _make_args(version)})


def _ensure_bossac(output_dir: Path) -> Path:
    """Return a working bossac binary in `output_dir`, built as currently configured."""
    version = _bossac_version()
    output_dir = output_dir.resolve()
    existing = output_dir / BOSSAC_NAME
    if existing.exists():
        try:
            stamp = output_dir.joinpath(STAMP_NAME).read_text()
            if stamp != _build_stamp(version):
                raise RuntimeError('built with a different configuration')
            if system() != 'Windows' and not os.access(existing, os.X_OK):
                existing.chmod(0o755)
            _check_version(existing, version)
            return existing
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            print(f'{existing} is stale or broken ({e!r}), rebuilding...')
    return build_bossac(output_dir)


def pdm_build_initialize(context: Context) -> None:
    """Set the wheel's tags and add the bossac binary to its scripts."""
    if context.target == 'sdist':
        return
    context.config_settings['--python-tag'] = 'py3'
    context.config_settings['--py-limited-api'] = 'none'

    if system() == 'Darwin':
        plat_version = MACOS_DEPLOYMENT_TARGET.replace('.', '_')
        context.config_settings['--plat-name'] = f'macosx_{plat_version}_{machine()}'
    elif system() == 'Linux' and (libc := libc_ver())[0] == 'glibc':
        glibc_major, glibc_minor = libc[1].split('.')[:2]
        plat_name = f'manylinux_{glibc_major}_{glibc_minor}_{machine()}'
        context.config_settings['--plat-name'] = plat_name

    bossac = _ensure_bossac(ROOT / 'bin')

    wheel_data = context.config.build_config.get('wheel-data', {})
    wheel_data['scripts'] = [
        {
            'path': bossac.relative_to(ROOT).as_posix(),
            'relative-to': bossac.parent.relative_to(ROOT).as_posix(),
        }
    ]
    context.config.build_config['wheel-data'] = wheel_data


if __name__ == '__main__':
    _ensure_bossac(ROOT / 'bin')
