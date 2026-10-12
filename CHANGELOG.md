# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

## [0.1.1]

### Added

- `THIRD_PARTY_NOTICES.md` with the license of BOSSA, shipped with the wheels as required by
  the BSD-3-Clause license of the bundled bossac binary
- License section in the README, clarifying that the MIT License covers only
  bossac-wrapper's own code, not the bundled bossac binary

### Changed

- The package's license metadata is now the SPDX expression `MIT AND BSD-3-Clause`

## [0.1.0]

_First release._

### Added

- `upload()`, `verify()`, `read()`, `erase()`, `reset()` and `info()` for programming devices
  with bossac, including support for Intel HEX files and the Arduino 1200 baud touch
- `version()` for querying the version of the bundled bossac binary
- `BossacError`, raised whenever bossac fails
- bossac 1.9.1, built from source and bundled with the wheels for Linux, macOS and Windows


[Unreleased]: https://github.com/int-brain-lab/bossac-wrapper/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/int-brain-lab/bossac-wrapper/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/int-brain-lab/bossac-wrapper/releases/tag/v0.1.0
