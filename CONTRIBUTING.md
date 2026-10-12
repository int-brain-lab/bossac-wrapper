Contributing
============

uv
--

This project uses [uv](https://docs.astral.sh/uv/) as its package manager, for managing
dependencies and ensuring consistent, reproducible environments. See uv's
[installation instructions](https://docs.astral.sh/uv/getting-started/installation/) for
details on installing it.

Installing developer dependencies
----------------------------------

```shell
uv sync --group dev
```

Building bossac
--------------

The bundled `bossac` binary isn't committed to this repository - it's built from source
automatically whenever it's needed (e.g. by `uv sync`, `uv build`, or `uv run tox`): the first
time, and again whenever the pinned version (`_BOSSAC_VERSION` in `bossac/__init__.py`) changes.
The build checks that BOSSA's release tag still points to the pinned commit (`_BOSSAC_COMMIT`) -
update both together. Building it requires `git`, `make` and a C++ compiler; on Windows, it
has to be built in an [MSYS2](https://www.msys2.org/) MINGW64 environment.

Running the test suite
-----------------------

Tests, linting, and type checking all run through [tox](https://tox.wiki/) (via the
[tox-uv](https://github.com/tox-dev/tox-uv) plugin), across every supported Python version -
uv provisions whichever interpreters are missing automatically:

```shell
uv run tox -p
```

Coverage report
---------------

After running the test suite, a merged coverage report is available via:

```shell
uv run coverage report
```

Checking and formatting code
-----------------------------

```shell
uv run ruff format
uv run ruff check --fix
uv run mypy
```

Building the documentation
---------------------------

```shell
uv run sphinx-build -b dirhtml docs/source docs/build
```

Building the package
---------------------

```shell
uv build --sdist
uv build --wheel
```

Build the wheel separately: a plain `uv build` builds it from the sdist, which doesn't contain
the bossac binary - so bossac would be cloned and compiled again every time.