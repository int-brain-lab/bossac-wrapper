"""A Python wrapper for bossac."""

import errno
import logging
import os
import re
import shlex
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from functools import cache, partial
from importlib.metadata import PackageNotFoundError, files
from os import PathLike, strerror
from pathlib import Path
from subprocess import PIPE, CalledProcessError, Popen
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import IO, NamedTuple, TypedDict

import serial
from serial.tools.list_ports import comports
from serial.tools.list_ports_common import ListPortInfo

log = logging.getLogger(__name__)

__version__ = '0.1.0'
"""Version of bossac-wrapper."""
_BOSSAC_VERSION = '1.9.1'
"""Version of the bundled bossac binary."""
_BOSSAC_COMMIT = '4bd2e659ad7255592923c5544f683a0ab9e46774'
"""Commit of BOSSA's release tag that the bundled bossac binary is built from."""
_BOSSAC_NAME = 'bossac.exe' if sys.platform == 'win32' else 'bossac'
"""File name of the bossac binary."""
_VALID_FIRMWARE_EXT = ('.bin', '.hex')
"""File extensions of supported firmware files."""
_RE_PROGRESS = re.compile(r'^\[[= ]*\] \d+% \(\d+/\d+ pages\)$')
"""Matches bossac's progress bar."""
_RE_VERSION = re.compile(r'\bVersion (\S+)')
"""Matches the version in bossac's help text."""
_RE_NO_BOOTLOADER = re.compile(
    r'^(No device found on |SAM-BA operation failed)', re.MULTILINE
)
"""Matches bossac's errors when no SAM-BA bootloader answers."""
_HEX_PAYLOAD_SIZES = {0x00: None, 0x01: 0, 0x02: 2, 0x03: 4, 0x04: 2, 0x05: 4}
"""Payload sizes of the Intel HEX record types, None for any size."""
_MAX_HEX_SPAN = 16 * 1024 * 1024
"""Largest address range of an Intel HEX file that is converted to a binary.

Guards against allocating a huge buffer for a sparse file, before the device's actual
flash size is known.
"""
_TOUCH_SETTLE = 2.0
"""Seconds after which a port that never disappeared is taken to be the bootloader's."""
_TOUCH_TIMEOUT = 10.0
"""Seconds to wait for the bootloader's port to appear after a 1200 baud touch."""
_TOUCH_POLL = 0.25
"""Seconds between checks for the bootloader's port after a 1200 baud touch."""
_BOSSAC_TIMEOUT = 30.0
"""Seconds without any output after which bossac is taken to be stuck and killed."""


class BossacError(CalledProcessError):
    """Raised when bossac exits with an error."""

    hint: str | None = None
    """A likely cause of the error, or :py:obj:`None` if there is none."""

    def __str__(self) -> str:
        """Return bossac's error message or the exit status, followed by the hint."""
        lines = (self.stderr or '').splitlines()
        message = '\n'.join(line.strip() for line in lines).strip()
        message = message or super().__str__()
        return f'{message}\n{self.hint}' if self.hint else message


class DeviceInfo(TypedDict):
    """A device's details as reported by bossac."""

    device: str
    """Name of the device, e.g. ``'ATSAMD21x18'``."""
    version: str
    """Version of the SAM-BA bootloader."""
    address: int
    """Address of the start of the flash."""
    pages: int
    """Number of flash pages."""
    page_size: int
    """Size of a flash page in bytes."""
    total_size: int
    """Size of the flash in bytes."""
    planes: int
    """Number of flash planes."""
    lock_regions: int
    """Number of lock regions."""
    locked: list[int]
    """Indices of the locked regions."""
    security: bool
    """Whether the flash security bit is set."""
    boot_flash: bool | None
    """Whether the device boots from flash, or :py:obj:`None` if not supported."""
    brownout_detection: bool | None
    """Whether brownout detection is on, or :py:obj:`None` if not supported."""
    brownout_reset: bool | None
    """Whether brownout reset is on, or :py:obj:`None` if not supported."""


class _Firmware(NamedTuple):
    """A firmware file and its contents as a binary."""

    path: Path
    """Path of the file."""
    data: bytes
    """Contents as a binary."""
    start: int | None
    """Start address of an Intel HEX file, None for a binary file."""


def upload(
    filename: PathLike | str,
    *,
    port: str,
    offset: int | None = None,
    erase: bool = True,
    verify: bool = True,
    boot: bool = True,
    reset: bool = True,
    touch: bool = True,
    usb: bool | None = None,
    log_level: int = logging.INFO,
) -> None:
    """
    Upload firmware to a device.

    The firmware file is checked first. Then, unless ``touch`` is False, the device is
    put into its SAM-BA bootloader with a 1200 baud touch. The flash is then erased,
    written and verified, the device is set to boot from flash and finally reset to
    start the firmware.

    Parameters
    ----------
    filename : PathLike | str
        Path to the firmware file: a raw binary (``.bin``) or an Intel HEX file
        (``.hex``).
    port : str
        Port of the device.
    offset : int, optional
        Flash offset at which to write a binary file - required for one. Must be aligned
        to a flash page, and must not overlap the bootloader - see the notes below. For
        an Intel HEX file, the offset is determined from the file's addresses and the
        device's flash address - specifying it raises a :py:exc:`ValueError`.
    erase : bool, default: True
        Erase the flash before writing.
    verify : bool, default: True
        Verify the flash's contents after writing.
    boot : bool, default: True
        Set the device to boot from flash, if supported.
    reset : bool, default: True
        Reset the device after the upload, if supported.
    touch : bool, default: True
        Put the device into its bootloader with a 1200 baud touch first.
    usb : bool, optional
        Whether the port is a native USB port (True) or a USB-to-serial bridge (False),
        e.g. the Arduino Due's programming port. Detected by bossac if not given.
    log_level : int, default: :py:data:`logging.INFO`
        Log level for bossac's status messages, :py:data:`logging.NOTSET` to disable.
        Errors are always logged as warnings.

    Raises
    ------
    FileNotFoundError
        If the firmware file does not exist, or if ``touch`` is True and the port can't
        be found.
    IsADirectoryError
        If the firmware path is a directory.
    ValueError
        If the firmware file has an unsupported extension, can't be loaded or doesn't
        fit into the flash, or if the offset is missing or invalid.
    TimeoutError
        If the bootloader's port doesn't appear after the 1200 baud touch, or if bossac
        stops responding, e.g. because the device isn't in its bootloader.
    BossacError
        If bossac fails, e.g. if the verification fails.

    Notes
    -----
    A binary file doesn't record where in the flash it belongs, so its offset has to be
    given, matching the board's bootloader. Offsets are relative to the start of the
    flash:

    - Arduino Due and other SAM3/SAM4 boards: 0, as their bootloader is in ROM
    - SAMD21 boards with an Arduino-style bootloader (Arduino Zero/MKR, SparkFun,
      Adafruit M0): ``0x2000``, after the 8 KiB bootloader
    - SAMD51 boards with a UF2 bootloader (Adafruit M4): ``0x4000``, after the
      16 KiB bootloader

    An offset of 0 on a board whose bootloader is in flash overwrites the bootloader.
    Intel HEX files carry absolute addresses, so they are flashed at the right offset
    on any board - prefer them where a build produces them.

    Examples
    --------
    Upload an Intel HEX file, which is flashed at the offset of its addresses:

    >>> bossac.upload('blink.hex', port='/dev/ttyACM0')

    Upload a binary file to a SAMD21 board, after its 8 KiB bootloader:

    >>> bossac.upload('blink.bin', port='/dev/ttyACM0', offset=0x2000)

    Upload a binary file via the programming port of an Arduino Due:

    >>> bossac.upload('blink.bin', port='/dev/ttyACM0', offset=0, usb=False)
    """
    firmware = _load_firmware(filename, offset)  # before the touch can harm the device
    if touch:
        port = _touch_1200(port)
    with _flashable(firmware, offset, port=port, usb=usb) as (path, flash_offset):
        args = ['--erase'] if erase else []
        args.append('--write')
        if verify:
            args.append('--verify')
        if boot:
            args.append('--boot=1')
        if reset:
            args.append('--reset')
        args.extend([f'--offset={flash_offset}', str(path)])
        _call_bossac(args, port=port, usb=usb, log_level=log_level)


def verify(
    filename: PathLike | str,
    *,
    port: str,
    offset: int | None = None,
    usb: bool | None = None,
    log_level: int = logging.INFO,
) -> None:
    """
    Verify that a device's flash matches a firmware file.

    The device must already be in its SAM-BA bootloader. There is deliberately no
    ``touch`` option: a 1200 baud touch usually destroys the very firmware that is to be
    checked. Ways into the bootloader that keep the firmware intact:

    - double-tapping the reset button, on SAMD boards with an Arduino-style bootloader
    - a command of the firmware itself that resets into the bootloader without erasing
      anything, where the firmware offers one
    - none on the Arduino Due, whose ROM bootloader is only reached by erasing the flash

    Parameters
    ----------
    filename : PathLike | str
        Path to the firmware file: a raw binary (``.bin``) or an Intel HEX file
        (``.hex``).
    port : str
        Port of the device.
    offset : int, optional
        Flash offset of a binary file - required for one, see :func:`upload` for
        typical values. For an Intel HEX file, the offset is determined from the file's
        addresses and the device's flash address - specifying it raises a
        :py:exc:`ValueError`.
    usb : bool, optional
        Whether the port is a native USB port (True) or a USB-to-serial bridge (False).
        Detected by bossac if not given.
    log_level : int, default: :py:data:`logging.INFO`
        Log level for bossac's status messages, :py:data:`logging.NOTSET` to disable.
        Errors are always logged as warnings.

    Raises
    ------
    FileNotFoundError
        If the firmware file does not exist.
    IsADirectoryError
        If the firmware path is a directory.
    ValueError
        If the firmware file has an unsupported extension, can't be loaded or doesn't
        fit into the flash, or if the offset is missing or invalid.
    TimeoutError
        If bossac stops responding, e.g. because the device isn't in its bootloader.
    BossacError
        If bossac fails or the flash doesn't match the firmware file.

    Examples
    --------
    Check the firmware of the device on a specific port:

    >>> bossac.verify('blink.bin', port='/dev/ttyACM0')
    """
    firmware = _load_firmware(filename, offset)
    with _flashable(firmware, offset, port=port, usb=usb) as (path, flash_offset):
        args = ['--verify', f'--offset={flash_offset}', str(path)]
        _call_bossac(args, port=port, usb=usb, log_level=log_level)


def read(
    filename: PathLike | str,
    *,
    port: str,
    size: int | None = None,
    offset: int = 0,
    usb: bool | None = None,
    log_level: int = logging.INFO,
) -> None:
    """
    Read a device's flash into a binary file.

    An existing file is overwritten.

    The device must already be in its SAM-BA bootloader. There is deliberately no
    ``touch`` option: a 1200 baud touch usually destroys the very firmware that is to be
    read. Ways into the bootloader that keep the firmware intact:

    - double-tapping the reset button, on SAMD boards with an Arduino-style bootloader
    - a command of the firmware itself that resets into the bootloader without erasing
      anything, where the firmware offers one
    - none on the Arduino Due, whose ROM bootloader is only reached by erasing the flash

    Parameters
    ----------
    filename : PathLike | str
        Path of the binary file (``.bin``) to write.
    port : str
        Port of the device.
    size : int, optional
        Number of bytes to read, the entire flash if not given.
    offset : int, default: 0
        Flash offset at which to start reading. Must be aligned to a flash page.
    usb : bool, optional
        Whether the port is a native USB port (True) or a USB-to-serial bridge (False).
        Detected by bossac if not given.
    log_level : int, default: :py:data:`logging.INFO`
        Log level for bossac's status messages, :py:data:`logging.NOTSET` to disable.
        Errors are always logged as warnings.

    Raises
    ------
    FileNotFoundError
        If the file's directory does not exist.
    IsADirectoryError
        If the path is a directory.
    ValueError
        If the file doesn't have the extension ``.bin``.
    TimeoutError
        If bossac stops responding, e.g. because the device isn't in its bootloader.
    BossacError
        If bossac fails.

    Examples
    --------
    Back up the first 64 KiB of the device's flash:

    >>> bossac.read('backup.bin', port='/dev/ttyACM0', size=0x10000)
    """
    filepath = Path(filename).absolute()
    if filepath.is_dir():
        raise IsADirectoryError(errno.EISDIR, strerror(errno.EISDIR), str(filepath))
    if not filepath.parent.is_dir():
        raise FileNotFoundError(
            errno.ENOENT, strerror(errno.ENOENT), str(filepath.parent)
        )
    if filepath.suffix.lower() != '.bin':
        raise ValueError(f"'{filepath.name}' must have the extension .bin")
    args = [
        '--read' if size is None else f'--read={size}',
        f'--offset={offset}',
        str(filepath),
    ]
    _call_bossac(args, port=port, usb=usb, log_level=log_level)


def erase(
    *,
    port: str,
    offset: int,
    touch: bool = False,
    usb: bool | None = None,
    log_level: int = logging.INFO,
) -> None:
    """
    Erase a device's flash.

    The device must be in its SAM-BA bootloader, unless ``touch`` is True.

    Parameters
    ----------
    port : str
        Port of the device.
    offset : int
        Flash offset at which to start erasing. Must be aligned to a flash page. On
        boards whose bootloader is in flash, it must not overlap the bootloader - see
        the notes of :func:`upload` for typical values.
    touch : bool, default: False
        Put the device into its bootloader with a 1200 baud touch first.
    usb : bool, optional
        Whether the port is a native USB port (True) or a USB-to-serial bridge (False).
        Detected by bossac if not given.
    log_level : int, default: :py:data:`logging.INFO`
        Log level for bossac's status messages, :py:data:`logging.NOTSET` to disable.
        Errors are always logged as warnings.

    Raises
    ------
    FileNotFoundError
        If ``touch`` is True and the port can't be found.
    TimeoutError
        If the bootloader's port doesn't appear after the 1200 baud touch, or if bossac
        stops responding, e.g. because the device isn't in its bootloader.
    BossacError
        If bossac fails.

    Warnings
    --------
    With an offset of 0, the entire flash is erased - including the bootloader of
    boards that keep it in flash, such as SAMD21 and SAMD51 boards. Such a board can't
    be programmed over USB anymore until its bootloader is restored with a debugger.

    Examples
    --------
    Erase the firmware of a SAMD21 board, keeping its 8 KiB bootloader:

    >>> bossac.erase(port='/dev/ttyACM0', offset=0x2000, touch=True)

    Erase the entire flash of an Arduino Due, whose bootloader is in ROM:

    >>> bossac.erase(port='/dev/ttyACM0', offset=0, touch=True)
    """
    if touch:
        port = _touch_1200(port)
    _call_bossac(
        ['--erase', f'--offset={offset}'], port=port, usb=usb, log_level=log_level
    )


def reset(
    *,
    port: str,
    usb: bool | None = None,
    log_level: int = logging.INFO,
) -> None:
    """
    Reset a device to start its firmware.

    The device must be in its SAM-BA bootloader.

    Parameters
    ----------
    port : str
        Port of the device.
    usb : bool, optional
        Whether the port is a native USB port (True) or a USB-to-serial bridge (False).
        Detected by bossac if not given.
    log_level : int, default: :py:data:`logging.INFO`
        Log level for bossac's status messages, :py:data:`logging.NOTSET` to disable.
        Errors are always logged as warnings.

    Raises
    ------
    TimeoutError
        If bossac stops responding, e.g. because the device isn't in its bootloader.
    BossacError
        If bossac fails.

    Examples
    --------
    Leave the bootloader of a device:

    >>> bossac.reset(port='/dev/ttyACM0')
    """
    _call_bossac(['--reset'], port=port, usb=usb, log_level=log_level)


def info(
    *,
    port: str,
    touch: bool = False,
    usb: bool | None = None,
) -> DeviceInfo:
    """
    Return a device's details.

    The device must be in its SAM-BA bootloader, unless ``touch`` is True. See
    :class:`DeviceInfo` for the details reported.

    Parameters
    ----------
    port : str
        Port of the device.
    touch : bool, default: False
        Put the device into its bootloader with a 1200 baud touch first. This
        usually destroys the firmware, which then has to be uploaded again: the
        Arduino Due erases its flash, and SAMD boards running Arduino firmware erase
        its first flash row.
    usb : bool, optional
        Whether the port is a native USB port (True) or a USB-to-serial bridge (False).
        Detected by bossac if not given.

    Returns
    -------
    DeviceInfo
        Details of the device.

    Raises
    ------
    FileNotFoundError
        If ``touch`` is True and the port can't be found.
    TimeoutError
        If the bootloader's port doesn't appear after the 1200 baud touch, or if bossac
        stops responding, e.g. because the device isn't in its bootloader.
    RuntimeError
        If bossac's output can't be parsed.
    BossacError
        If bossac fails.

    Examples
    --------
    Get the flash size of a device in its bootloader:

    >>> bossac.info(port='/dev/ttyACM0')['total_size']
    524288
    """
    if touch:
        port = _touch_1200(port)
    output = _call_bossac(['--info'], port=port, usb=usb)
    return _parse_info(output)


def version() -> str:
    """
    Return the version of the bundled bossac binary.

    Returns
    -------
    str
        Version of bossac, e.g. ``'1.9.1'``.

    Raises
    ------
    RuntimeError
        If the version can't be determined from bossac's output.
    TimeoutError
        If bossac stops responding, e.g. because the device isn't in its bootloader.
    BossacError
        If bossac fails.

    Examples
    --------
    Check the version of the bundled bossac binary:

    >>> bossac.version()
    '1.9.1'
    """
    # bossac has no --version option - its help text names the version, exit status 1
    output = _call_bossac(['--help'], returncodes=(0, 1))
    if (match := _RE_VERSION.search(output)) is None:
        raise RuntimeError('Could not determine the version of bossac')
    return match.group(1)


def _parse_info(output: str) -> DeviceInfo:
    """Return the details printed by ``bossac --info``."""
    fields = {}
    for line in output.splitlines():
        key, sep, value = line.partition(':')
        if sep:
            fields[key.strip().lower()] = value.strip()
    try:
        locked = fields['locked']
        return DeviceInfo(
            device=fields['device'],
            version=fields['version'],
            address=int(fields['address'], 16),
            pages=int(fields['pages']),
            page_size=int(fields['page size'].removesuffix('bytes')),
            total_size=int(fields['total size'].removesuffix('KB')) * 1024,
            planes=int(fields['planes']),
            lock_regions=int(fields['lock regions']),
            locked=[] if locked == 'none' else [int(r) for r in locked.split(',')],
            security=fields['security'] == 'true',
            boot_flash=_parse_optional_bool(fields.get('boot flash')),
            brownout_detection=_parse_optional_bool(fields.get('bod')),
            brownout_reset=_parse_optional_bool(fields.get('bor')),
        )
    except (KeyError, ValueError) as e:
        raise RuntimeError("Could not parse bossac's device information") from e


def _parse_optional_bool(value: str | None) -> bool | None:
    """Return a boolean printed by bossac, or None if it wasn't printed."""
    return None if value is None else value == 'true'


def _parse_firmware_file(filename: PathLike | str) -> Path:
    """Return the absolute path of a firmware file, or raise if it's not usable."""
    filepath = Path(filename).absolute()
    if not filepath.exists():
        raise FileNotFoundError(errno.ENOENT, strerror(errno.ENOENT), str(filepath))
    if filepath.is_dir():
        raise IsADirectoryError(errno.EISDIR, strerror(errno.EISDIR), str(filepath))
    if filepath.suffix.lower() not in _VALID_FIRMWARE_EXT:
        raise ValueError(
            f"'{filepath.name}' has unrecognized extension "
            f'(supported: {", ".join(_VALID_FIRMWARE_EXT)})'
        )
    return filepath


def _load_firmware(filename: PathLike | str, offset: int | None) -> _Firmware:
    """Load a firmware file, or raise if it's not usable - before touching a device."""
    filepath = _parse_firmware_file(filename)
    if filepath.suffix.lower() == '.bin':
        if offset is None:
            raise ValueError(
                f"'{filepath.name}' doesn't record where in the flash it belongs - "
                'pass offset, e.g. 0x2000 after an 8 KiB Arduino SAMD21 bootloader, '
                'or 0 on an Arduino Due'
            )
        return _Firmware(filepath, filepath.read_bytes(), start=None)
    if offset is not None:
        raise ValueError('offset is determined from the addresses of an Intel HEX file')
    return _Firmware(filepath, *_read_hex(filepath))


@contextmanager
def _flashable(
    firmware: _Firmware, offset: int | None, *, port: str, usb: bool | None
) -> Iterator[tuple[Path, int]]:
    """Yield the path of a firmware file as a binary, and the offset to flash it at."""
    # bossac's offsets are relative to the start of the device's flash, and it checks
    # an image's size without its offset - after it has erased the flash
    device = info(port=port, usb=usb)
    name = firmware.path.name
    if offset is None:  # an Intel HEX file, whose addresses are absolute
        assert firmware.start is not None
        offset = firmware.start - device['address']
    address = device['address'] + offset
    if offset < 0:
        raise ValueError(
            f"'{name}' starts at 0x{address:x}, "
            f'before the flash at 0x{device["address"]:x}'
        )
    if offset % device['page_size']:
        raise ValueError(
            f"'{name}' starts at 0x{address:x}, which isn't aligned to a flash "
            f'page of {device["page_size"]} bytes'
        )
    if offset + len(firmware.data) > device['total_size']:
        raise ValueError(
            f"'{name}' doesn't fit into the flash: {len(firmware.data)} bytes at "
            f'offset 0x{offset:x}, but the flash holds {device["total_size"]} bytes'
        )
    if firmware.start is None:
        yield firmware.path, offset
        return
    with TemporaryDirectory() as tmp:
        binary = Path(tmp, firmware.path.with_suffix('.bin').name)
        binary.write_bytes(firmware.data)
        yield binary, offset


def _read_hex(filepath: Path) -> tuple[bytes, int]:
    """Return the contents of an Intel HEX file as a binary, and its start address."""
    chunks: dict[int, bytes] = {}
    base = 0
    with filepath.open() as f:
        for number, line in enumerate(f, start=1):
            if not (line := line.strip()):
                continue
            try:
                if not line.startswith(':'):
                    raise ValueError('missing start code')
                record = bytes.fromhex(line[1:])
                if len(record) < 5 or len(record) != record[0] + 5:
                    raise ValueError('invalid length')
                if sum(record) & 0xFF:
                    raise ValueError('invalid checksum')
                if record[3] not in _HEX_PAYLOAD_SIZES:
                    raise ValueError(f'unknown record type 0x{record[3]:02x}')
                size = _HEX_PAYLOAD_SIZES[record[3]]
                if size is not None and record[0] != size:
                    raise ValueError(
                        f'invalid length for record type 0x{record[3]:02x}'
                    )
            except ValueError as e:
                raise ValueError(
                    f"Invalid record in '{filepath.name}', line {number}: {e}"
                ) from e
            address = int.from_bytes(record[1:3], 'big')
            record_type, payload = record[3], record[4:-1]
            if record_type == 0x00:  # data
                chunks[base + address] = payload
            elif record_type == 0x01:  # end of file
                break
            elif record_type == 0x02:  # extended segment address
                base = int.from_bytes(payload, 'big') << 4
            elif record_type == 0x04:  # extended linear address
                base = int.from_bytes(payload, 'big') << 16
            # 0x03 & 0x05 (start addresses) don't affect the flash's contents
    if not chunks:
        raise ValueError(f"'{filepath.name}' contains no data")

    start = min(chunks)
    end = max(address + len(payload) for address, payload in chunks.items())
    if end - start > _MAX_HEX_SPAN:
        raise ValueError(
            f"'{filepath.name}' spans 0x{start:x} to 0x{end:x}, too much to be flashed"
        )
    data = bytearray(b'\xff') * (end - start)  # gaps are left as erased flash
    for address, payload in chunks.items():
        data[address - start : address - start + len(payload)] = payload
    return bytes(data), start


def _touch_1200(port: str) -> str:
    """Put a device into its bootloader and return the bootloader's port."""
    ports = comports()
    if (device := _find_port(port, ports)) is None:
        raise FileNotFoundError(f'Could not find serial port {port}')
    before = {p.device for p in ports}

    # opening the port at 1200 baud and dropping DTR resets the device into its
    # bootloader - and usually destroys its firmware: the Arduino Due erases its flash,
    # Arduino firmware on SAMD boards erases its own first flash row
    log.debug('Touching %s at 1200 baud', device.device)
    with serial.Serial(device.device, 1200) as connection:
        connection.dtr = False

    # a native USB port disappears, the bootloader's then appears at the same USB
    # location - possibly with a different name. The port of a USB-to-serial bridge
    # stays where it is.
    start = time.monotonic()
    disappeared = False
    while (elapsed := time.monotonic() - start) < _TOUCH_TIMEOUT:
        time.sleep(_TOUCH_POLL)
        listed = {p.device: p for p in comports()}
        if new := [
            name
            for name, p in sorted(listed.items())
            if name not in before and _same_device(p, device)
        ]:
            log.debug('Bootloader appeared on %s', new[0])
            return new[0]
        before &= set(listed)  # so that a port reappearing under its old name is new
        if device.device not in listed:
            disappeared = True
        elif not disappeared and elapsed >= _TOUCH_SETTLE:
            return device.device
    raise TimeoutError(f'No bootloader appeared after touching {port} at 1200 baud')


def _find_port(port: str, ports: Iterable[ListPortInfo]) -> ListPortInfo | None:
    """Return the port with the given name or an alias of it."""
    key = _port_key(port)
    return next((p for p in ports if _port_key(p.device) == key), None)


def _same_device(port: ListPortInfo, device: ListPortInfo) -> bool:
    """Return whether a port belongs to a device - by USB location, else by name."""
    if (location := _usb_location(device)) is not None:
        return _usb_location(port) == location
    return port.device == device.device


def _port_key(port: str) -> str:
    """Return a port's name as listed by pyserial, with aliases resolved."""
    if sys.platform == 'win32':
        return port.upper().removeprefix('\\\\.\\')
    port = os.path.realpath(port)  # e.g. /dev/serial/by-id/...
    if sys.platform == 'darwin':
        port = port.replace('/dev/tty.', '/dev/cu.', 1)  # listed as call-out devices
    return port


def _usb_location(port: ListPortInfo) -> str | None:
    """Return the USB location of a port's device, without its interface."""
    return port.location.partition(':')[0] if port.location else None


def _call_bossac(
    args: list[str],
    *,
    port: str | None = None,
    usb: bool | None = None,
    log_level: int = logging.NOTSET,
    returncodes: tuple[int, ...] = (0,),
) -> str:
    """Run bossac, log its output and return its stdout."""
    args = _assemble_args(args, port=port, usb=usb)
    if log.isEnabledFor(logging.DEBUG):
        log.debug('Calling subprocess: %s', shlex.join(args))

    # stdout (status messages) is logged at log_level, stderr (errors) at WARNING -
    # stderr is read in a thread, so neither pipe can fill up and block bossac.
    # Progress bars are separated by carriage returns, which text mode turns into
    # line breaks.
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    stderr_errors: list[Exception] = []
    finished, timed_out = Event(), Event()
    with Popen(args, stdout=PIPE, stderr=PIPE, encoding='utf-8', errors='replace') as p:
        assert p.stdout is not None
        assert p.stderr is not None
        stderr_thread = Thread(
            target=partial(_consume_stderr, p, p.stderr, stderr_lines, stderr_errors),
            daemon=True,  # in case joining it after an error is interrupted, too
        )
        watchdog = Thread(
            target=partial(
                _watch, p, (stdout_lines, stderr_lines), finished, timed_out
            ),
            daemon=True,
        )
        stderr_thread.start()
        watchdog.start()
        try:
            _consume_pipe(p.stdout, stdout_lines, log_level)
            stderr_thread.join()  # before the with-block closes the pipes
        except BaseException:
            p.kill()  # bossac may take a while to give up on an unresponsive device
            stderr_thread.join()
            raise
        finally:
            finished.set()
            watchdog.join()
    if timed_out.is_set():
        raise TimeoutError(
            f'bossac stopped responding for {_BOSSAC_TIMEOUT:g} s and was killed - the '
            'device may not be in its SAM-BA bootloader, or may have been reset or '
            'disconnected'
        )
    if stderr_errors:
        raise stderr_errors[0]
    stdout = '\n'.join(stdout_lines)
    stderr = '\n'.join(stderr_lines)

    if p.returncode not in returncodes:
        if not stderr and 'Verify failed' in stdout:
            # bossac reports a failed verification on stdout
            stderr = stdout[stdout.index('Verify failed') :]
        error = BossacError(p.returncode, p.args, output=stdout, stderr=stderr)
        if port is not None and _RE_NO_BOOTLOADER.search(stderr):
            error.hint = (
                f'The device on {port} may not be in its SAM-BA bootloader - put it '
                'there first, e.g. with a 1200 baud touch (touch=True) or by '
                'double-tapping its reset button.'
            )
        raise error
    return stdout


def _watch(
    process: Popen[str],
    outputs: tuple[list[str], ...],
    finished: Event,
    timed_out: Event,
) -> None:
    """Kill bossac if its outputs stop growing - bossac itself never times out."""
    count, since = -1, time.monotonic()
    while not finished.wait(min(_BOSSAC_TIMEOUT / 10, 1.0)):
        if (current := sum(map(len, outputs))) != count:
            count, since = current, time.monotonic()
        elif time.monotonic() - since > _BOSSAC_TIMEOUT:
            timed_out.set()
            process.kill()
            return


def _consume_pipe(stream: IO[str], lines: list[str], log_level: int) -> None:
    """Collect each line as is and log it - progress bars at DEBUG, if at all."""
    for line in stream:
        lines.append(line := line.rstrip('\n'))
        if (message := line.strip()) and log_level > logging.NOTSET:
            level = logging.DEBUG if _RE_PROGRESS.match(message) else log_level
            log.log(level, message)


def _consume_stderr(
    process: Popen[str], stream: IO[str], lines: list[str], errors: list[Exception]
) -> None:
    """Consume stderr in a thread - on failure, kill bossac and keep the error."""
    try:
        _consume_pipe(stream, lines, logging.WARNING)
    except Exception as e:  # noqa: BLE001 - re-raised by _call_bossac
        errors.append(e)
        process.kill()  # otherwise, bossac might block on a full stderr pipe


@cache
def _resolve_bossac() -> str:
    """Return the path of the bundled bossac binary."""
    try:
        record = files('bossac-wrapper') or []
    except PackageNotFoundError:
        record = []
    for file in record:
        if (
            file.name == _BOSSAC_NAME
            and (path := Path(file.locate()).resolve()).is_file()
        ):
            return str(path)
    raise FileNotFoundError(
        f'Could not find the {_BOSSAC_NAME} binary bundled with bossac-wrapper - '
        'try reinstalling the package'
    )


def _assemble_args(
    args: list[str],
    *,
    port: str | None = None,
    usb: bool | None = None,
) -> list[str]:
    """Return the bossac command line, with the port's options if a port is given."""
    output = [_resolve_bossac()]
    if port is not None:
        output.append(f'--port={port}')
        if usb is not None:
            output.append(f'--usb-port={int(usb)}')
    return [*output, *args]
