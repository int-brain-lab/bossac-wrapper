import errno
import logging
import sys
import threading
import time
from pathlib import Path
from subprocess import CalledProcessError, Popen
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from serial.tools.list_ports_common import ListPortInfo

import bossac

# arguments for running Python as a stand-in for bossac: isolated mode and no site
# import, so starting the interpreter is fast and unaffected by the environment
PYTHON_ARGS = ['-I', '-S', '-c']

# Blink for the SparkFun SAMD21 Mini (SparkFun:samd 1.8.13, Arduino IDE 1.8.19), as a
# binary and as an Intel HEX file of the same build - to be flashed after the 8 KiB
# bootloader, at 0x2000
BLINK_BIN = Path(__file__).parent.joinpath('Blink.ino.bin').resolve()
BLINK_HEX = Path(__file__).parent.joinpath('Blink.ino.hex').resolve()

# `bossac --info` output of an Arduino Due and a SparkFun SAMD21 Mini
INFO_DUE = [
    'Device       : ATSAM3X8',
    'Version      : v1.1 Dec 15 2010 19:25:04',
    'Address      : 0x80000',
    'Pages        : 2048',
    'Page Size    : 256 bytes',
    'Total Size   : 512KB',
    'Planes       : 2',
    'Lock Regions : 32',
    'Locked       : none',
    'Security     : false',
    'Boot Flash   : true',
]
INFO_SAMD21 = [
    'Device       : ATSAMD21x18',
    'Version      : v1.1 [Arduino:XYZ] Feb 20 2019 10:43:51',
    'Address      : 0x0',
    'Pages        : 4096',
    'Page Size    : 64 bytes',
    'Total Size   : 256KB',
    'Planes       : 1',
    'Lock Regions : 16',
    'Locked       : none',
    'Security     : false',
    'BOD          : true',
    'BOR          : true',
]


@pytest.fixture(autouse=True)
def _clear_resolve_bossac_cache():
    # _resolve_bossac is cached, so a real resolution from one test would otherwise
    # leak into the next (e.g. into test_resolve_bossac's own mocked scenarios).
    bossac._resolve_bossac.cache_clear()
    yield
    bossac._resolve_bossac.cache_clear()


@pytest.fixture(autouse=True)
def _no_ports():
    # keep tests from finding - let alone touching - devices that are connected
    with patch('bossac.comports', return_value=[]):
        yield


@pytest.fixture
def mock_popen():
    with patch('bossac.Popen', spec=Popen) as mock_popen:
        context = mock_popen.return_value.__enter__.return_value

        def popen(args, **_):
            # keep the contents of firmware files passed to bossac, which may be
            # temporary
            if args[-1].endswith('.bin') and Path(args[-1]).is_file():
                mock_popen.files.append(Path(args[-1]).read_bytes())
            return mock_popen.return_value

        def set_pipes(stdout: list[str] | None = None, stderr: list[str] | None = None):
            # like real pipes, iterating yields lines that end with a newline
            context.stdout = [f'{line}\n' for line in stdout or []]
            context.stderr = [f'{line}\n' for line in stderr or []]

        def set_returncode(returncode: int = 0):
            context.returncode = returncode

        mock_popen.side_effect = popen
        mock_popen.set_pipes = set_pipes
        mock_popen.set_returncode = set_returncode
        mock_popen.files = []
        mock_popen.set_pipes([], [])
        mock_popen.set_returncode(0)

        yield mock_popen


@pytest.fixture
def mock_info():
    # the device queried before flashing, so that bossac is only called to flash it
    with patch('bossac.info', return_value=_device_info(INFO_SAMD21)) as mock_info:
        yield mock_info


@pytest.fixture
def mock_touch():
    with patch(
        'bossac._touch_1200', side_effect=lambda port: f'{port}-bootloader'
    ) as m:
        yield m


def _hex_record(record_type: int, address: int = 0, data: bytes = b'') -> str:
    record = bytes([len(data), address >> 8, address & 0xFF, record_type, *data])
    return f':{record.hex().upper()}{-sum(record) & 0xFF:02X}'


def _write_hex(path: Path, *records: str) -> Path:
    path.write_text('\n'.join([*records, _hex_record(0x01)]) + '\n')
    return path


def _device_info(stdout: list[str]) -> bossac.DeviceInfo:
    return bossac._parse_info('\n'.join(stdout))


def _port_info(device, vid=None, pid=None, location=None, description='n/a'):
    port = ListPortInfo(device, skip_link_detection=True)
    port.vid, port.pid, port.location, port.description = (
        vid,
        pid,
        location,
        description,
    )
    return port


def _args(mock_popen) -> list[str]:
    return mock_popen.call_args[0][0]


@pytest.mark.usefixtures('mock_info')
class TestUpload:
    def test_upload(self, mock_popen, mock_touch, mock_info, caplog):
        """Upload options are passed to bossac and status messages are logged."""
        mock_popen.set_pipes(stdout=['Erase flash'])
        caplog.set_level(logging.INFO)
        bossac.upload(BLINK_BIN, port='/dev/ttyACM0', offset=0x2000)
        mock_touch.assert_called_once_with('/dev/ttyACM0')
        # the device is queried in its bootloader, to check the firmware fits
        mock_info.assert_called_once_with(port='/dev/ttyACM0-bootloader', usb=None)
        assert _args(mock_popen)[1:] == [
            '--port=/dev/ttyACM0-bootloader',
            '--erase',
            '--write',
            '--verify',
            '--boot=1',
            '--reset',
            f'--offset={0x2000}',
            str(BLINK_BIN),
        ]
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ('INFO', 'Erase flash')
        ]

        caplog.clear()
        mock_touch.reset_mock()
        bossac.upload(
            BLINK_BIN,
            port='COM3',
            offset=0x2000,
            erase=False,
            verify=False,
            boot=False,
            reset=False,
            touch=False,
            usb=False,
            log_level=logging.NOTSET,
        )
        mock_touch.assert_not_called()
        assert _args(mock_popen)[1:] == [
            '--port=COM3',
            '--usb-port=0',
            '--write',
            f'--offset={0x2000}',
            str(BLINK_BIN),
        ]
        assert len(caplog.records) == 0

    def test_upload_hex(self, mock_popen, mock_touch, mock_info):
        """A HEX file is converted to a binary, flashed at an offset from the device."""
        bossac.upload(BLINK_HEX, port='/dev/ttyACM0')
        mock_touch.assert_called_once_with('/dev/ttyACM0')
        mock_info.assert_called_once_with(port='/dev/ttyACM0-bootloader', usb=None)
        assert mock_popen.files == [BLINK_BIN.read_bytes()]
        assert _args(mock_popen)[1] == '--port=/dev/ttyACM0-bootloader'
        assert f'--offset={0x2000}' in _args(mock_popen)
        binary = Path(_args(mock_popen)[-1])
        assert binary.name == 'Blink.ino.bin'
        assert not binary.exists()  # the temporary binary is removed

    @pytest.mark.parametrize(
        ('name', 'content', 'kwargs', 'error'),
        [
            ('missing.bin', None, {'offset': 0x2000}, FileNotFoundError),
            ('blink.bin', BLINK_BIN.read_bytes(), {}, ValueError),  # no offset
            ('blink.elf', b'', {}, ValueError),
            ('blink.hex', BLINK_HEX.read_bytes(), {'offset': 0x2000}, ValueError),
            ('corrupt.hex', b':0100000000FE\n', {}, ValueError),
        ],
    )
    def test_upload_checks_file_before_touch(
        self, mock_popen, mock_touch, tmp_path, name, content, kwargs, error
    ):
        """An unusable firmware file raises before the device is touched."""
        if content is not None:
            (tmp_path / name).write_bytes(content)
        with pytest.raises(error):
            bossac.upload(tmp_path / name, port='/dev/ttyACM0', **kwargs)
        mock_touch.assert_not_called()
        mock_popen.assert_not_called()

    @pytest.mark.parametrize(
        ('offset', 'message'),
        [
            (-0x100, 'before the flash'),
            (0x2010, "isn't aligned to a flash page of 64 bytes"),
            (0x3E000, "doesn't fit into the flash: 10288 bytes at offset 0x3e000"),
        ],
    )
    def test_upload_invalid_offset(self, mock_popen, offset, message):
        """A binary must start in the flash, at a page boundary, and fit into it."""
        with pytest.raises(ValueError, match=message):
            bossac.upload(BLINK_BIN, port='/dev/ttyACM0', offset=offset, touch=False)
        mock_popen.assert_not_called()  # nothing was erased or written

    @pytest.mark.parametrize(
        ('address', 'message'),
        [(0x7FF00, 'before the flash'), (0x80010, "isn't aligned")],
    )
    def test_upload_hex_invalid_address(
        self, mock_popen, mock_info, tmp_path, address, message
    ):
        """A HEX file must start within the flash and at a page boundary."""
        hex_file = _write_hex(
            tmp_path / 'firmware.hex',
            _hex_record(0x04, data=(address >> 16).to_bytes(2, 'big')),
            _hex_record(0x00, address & 0xFFFF, b'\x00'),
        )
        mock_info.return_value = _device_info(INFO_DUE)
        with pytest.raises(ValueError, match=message):
            bossac.upload(hex_file, port='/dev/ttyACM0', touch=False)
        mock_popen.assert_not_called()  # nothing was erased or written

    def test_upload_hex_too_large(self, mock_popen, tmp_path):
        """A HEX file that also holds data beyond the flash, e.g. fuses, is rejected."""
        hex_file = _write_hex(
            tmp_path / 'firmware.hex',
            _hex_record(0x00, 0x2000, b'\x00'),
            _hex_record(0x04, data=b'\x00\x80'),  # SAMD21 user row at 0x804000
            _hex_record(0x00, 0x4000, b'\x00'),
        )
        with pytest.raises(ValueError, match="doesn't fit into the flash"):
            bossac.upload(hex_file, port='/dev/ttyACM0', touch=False)
        mock_popen.assert_not_called()  # nothing was erased or written

    def test_upload_verify_failed(self, mock_popen):
        """A failed verification, reported by bossac on stdout, raises BossacError."""
        mock_popen.set_pipes(
            ['Verify 4 bytes of flash', '', 'Verify failed', 'Page errors: 1'], []
        )
        mock_popen.set_returncode(2)
        with pytest.raises(bossac.BossacError) as exc_info:
            bossac.upload(BLINK_BIN, port='/dev/ttyACM0', offset=0x2000, touch=False)
        assert exc_info.value.returncode == 2
        assert str(exc_info.value) == 'Verify failed\nPage errors: 1'


class TestVerify:
    def test_verify(self, mock_popen, mock_info):
        """Verify options are passed to bossac, after checking the firmware fits."""
        bossac.verify(BLINK_BIN, port='/dev/ttyACM0', offset=0)
        mock_info.assert_called_once_with(port='/dev/ttyACM0', usb=None)
        assert _args(mock_popen)[1:] == [
            '--port=/dev/ttyACM0',
            '--verify',
            '--offset=0',
            str(BLINK_BIN),
        ]
        bossac.verify(BLINK_BIN, port='/dev/ttyACM0', offset=0x2000, usb=True)
        assert _args(mock_popen)[1:] == [
            '--port=/dev/ttyACM0',
            '--usb-port=1',
            '--verify',
            f'--offset={0x2000}',
            str(BLINK_BIN),
        ]

    @pytest.mark.usefixtures('mock_info')
    def test_verify_hex(self, mock_popen):
        """A HEX file is verified as a binary at the offset of its addresses."""
        bossac.verify(BLINK_HEX, port='/dev/ttyACM0')
        assert mock_popen.files == [BLINK_BIN.read_bytes()]
        assert f'--offset={0x2000}' in _args(mock_popen)


class TestRequiredOffset:
    @pytest.mark.parametrize('function', [bossac.upload, bossac.verify])
    def test_bin_requires_offset(self, mock_popen, mock_touch, function):
        """A binary file without an offset is rejected before anything else."""
        with pytest.raises(ValueError, match="doesn't record where in the flash"):
            function(BLINK_BIN, port='/dev/ttyACM0')
        mock_touch.assert_not_called()
        mock_popen.assert_not_called()


class TestRead:
    def test_read(self, mock_popen, tmp_path):
        """Read options are passed to bossac."""
        filename = tmp_path / 'backup.bin'
        bossac.read(filename, port='/dev/ttyACM0')
        assert _args(mock_popen)[1:] == [
            '--port=/dev/ttyACM0',
            '--read',
            '--offset=0',
            str(filename),
        ]
        bossac.read(str(filename), port='/dev/ttyACM0', size=1024, offset=256)
        assert _args(mock_popen)[2:] == ['--read=1024', '--offset=256', str(filename)]

    def test_read_invalid_file(self, mock_popen, tmp_path):
        """The file must be a .bin file in an existing directory."""
        with pytest.raises(IsADirectoryError) as exc_info:
            bossac.read(tmp_path, port='/dev/ttyACM0')
        assert exc_info.value.errno == errno.EISDIR
        with pytest.raises(FileNotFoundError) as exc_info:
            bossac.read(tmp_path / 'missing' / 'backup.bin', port='/dev/ttyACM0')
        assert exc_info.value.errno == errno.ENOENT
        with pytest.raises(ValueError, match='must have the extension .bin'):
            bossac.read(tmp_path / 'backup.hex', port='/dev/ttyACM0')
        mock_popen.assert_not_called()


class TestErase:
    def test_erase(self, mock_popen, mock_touch):
        """Erase options are passed to bossac, after an optional touch."""
        bossac.erase(port='/dev/ttyACM0', offset=0)
        mock_touch.assert_not_called()
        assert _args(mock_popen)[1:] == ['--port=/dev/ttyACM0', '--erase', '--offset=0']

        bossac.erase(port='/dev/ttyACM0', offset=0x1000, touch=True)
        mock_touch.assert_called_once_with('/dev/ttyACM0')
        assert _args(mock_popen)[1:] == [
            '--port=/dev/ttyACM0-bootloader',
            '--erase',
            f'--offset={0x1000}',
        ]


class TestReset:
    def test_reset(self, mock_popen, caplog):
        """Reset is passed to bossac, and failures raise BossacError."""
        caplog.set_level(logging.INFO)
        bossac.reset(port='/dev/ttyACM0')
        assert _args(mock_popen)[1:] == ['--port=/dev/ttyACM0', '--reset']

        mock_popen.set_pipes([], ['', 'No device found on /dev/ttyACM0'])
        mock_popen.set_returncode(1)
        with pytest.raises(bossac.BossacError, match='^No device found on'):
            bossac.reset(port='/dev/ttyACM0')
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ('WARNING', 'No device found on /dev/ttyACM0')
        ]


class TestInfo:
    def test_info_due(self, mock_popen, mock_touch):
        """The device information of an Arduino Due is parsed."""
        mock_popen.set_pipes(INFO_DUE)
        assert bossac.info(port='/dev/ttyACM0') == {
            'device': 'ATSAM3X8',
            'version': 'v1.1 Dec 15 2010 19:25:04',
            'address': 0x80000,
            'pages': 2048,
            'page_size': 256,
            'total_size': 512 * 1024,
            'planes': 2,
            'lock_regions': 32,
            'locked': [],
            'security': False,
            'boot_flash': True,
            'brownout_detection': None,
            'brownout_reset': None,
        }
        assert _args(mock_popen)[1:] == ['--port=/dev/ttyACM0', '--info']
        mock_touch.assert_not_called()

    def test_info_samd21(self, mock_popen, mock_touch):
        """The device information of a SparkFun SAMD21 Mini is parsed, after a touch."""
        mock_popen.set_pipes(INFO_SAMD21)
        output = bossac.info(port='/dev/ttyACM0', touch=True, usb=True)
        mock_touch.assert_called_once_with('/dev/ttyACM0')
        assert _args(mock_popen)[1:] == [
            '--port=/dev/ttyACM0-bootloader',
            '--usb-port=1',
            '--info',
        ]
        assert output == {
            'device': 'ATSAMD21x18',
            'version': 'v1.1 [Arduino:XYZ] Feb 20 2019 10:43:51',
            'address': 0,
            'pages': 4096,
            'page_size': 64,
            'total_size': 256 * 1024,
            'planes': 1,
            'lock_regions': 16,
            'locked': [],
            'security': False,
            'boot_flash': None,
            'brownout_detection': True,
            'brownout_reset': True,
        }

    @pytest.mark.parametrize(
        ('locked', 'expected'), [('none', []), ('3', [3]), ('0,1,15', [0, 1, 15])]
    )
    def test_info_locked(self, mock_popen, locked, expected):
        """Locked regions are parsed into a list of indices."""
        stdout = [line for line in INFO_SAMD21 if not line.startswith('Locked')]
        mock_popen.set_pipes([*stdout, f'Locked       : {locked}'])
        assert bossac.info(port='/dev/ttyACM0')['locked'] == expected

    @pytest.mark.parametrize(
        'stdout', [[], INFO_DUE[:-3], [*INFO_DUE[:2], 'Address      : nope']]
    )
    def test_info_unparsable(self, mock_popen, stdout):
        """Missing or invalid fields raise RuntimeError."""
        mock_popen.set_pipes(stdout)
        with pytest.raises(RuntimeError, match="Could not parse bossac's device"):
            bossac.info(port='/dev/ttyACM0')


DUE = ('/dev/ttyACM0', 0x2341, 0x003E, '3-2:1.0')
DUE_SAMBA = ('/dev/ttyACM0', 0x03EB, 0x6124, '3-2:1.0')
DUE_BRIDGE = ('/dev/ttyACM0', 0x2341, 0x003D, '3-2:1.0')
OTHER_BOARD = ('/dev/ttyACM1', 0x2341, 0x804D, '3-3:1.0')


class TestTouch1200:
    @pytest.fixture(autouse=True)
    def _fast_touch(self):
        with (
            patch('bossac._TOUCH_POLL', 0.01),
            patch('bossac._TOUCH_SETTLE', 0.05),
            patch('bossac._TOUCH_TIMEOUT', 0.5),
        ):
            yield

    @pytest.fixture
    def mock_serial(self):
        """Patch serial.Serial - set_ports(before, *after) sets the listed ports:
        `before` until the touch, then each of `after`, the last one repeatedly."""
        state = {'before': [], 'after': [[]], 'touched': False, 'polls': 0}

        def comports():
            if not state['touched']:
                return [_port_info(*p) for p in state['before']]
            after = state['after'][min(state['polls'], len(state['after']) - 1)]
            state['polls'] += 1
            return [_port_info(*p) for p in after]

        def touch(*_):
            state['touched'] = True
            return mock_serial.return_value

        def set_ports(before, *after):
            state['before'], state['after'] = before, list(after) or [before]

        with (
            patch('bossac.comports', side_effect=comports),
            patch('bossac.serial.Serial', side_effect=touch) as mock_serial,
        ):
            mock_serial.set_ports = set_ports
            yield mock_serial

    def test_touch_reappearing_port(self, mock_serial):
        """A native USB port that disappears and reappears is the bootloader's."""
        mock_serial.set_ports([DUE], [DUE], [], [], [DUE_SAMBA])
        assert bossac._touch_1200('/dev/ttyACM0') == '/dev/ttyACM0'
        mock_serial.assert_called_once_with('/dev/ttyACM0', 1200)
        assert mock_serial.return_value.__enter__.return_value.dtr is False

    def test_touch_new_port(self, mock_serial):
        """A port appearing at the same USB location is the bootloader's, e.g. on
        Windows, where it gets a new name."""
        mock_serial.set_ports(
            [('COM3', 0x2341, 0x003E, '1-2:x.0'), ('COM1',)],
            [('COM1',)],
            [('COM1',), ('COM4', 0x03EB, 0x6124, '1-2:x.0')],
        )
        assert bossac._touch_1200('COM3') == 'COM4'

    def test_touch_ignores_other_boards(self, mock_serial):
        """A port appearing at another USB location belongs to another board."""
        mock_serial.set_ports([DUE], [], [OTHER_BOARD], [OTHER_BOARD, DUE_SAMBA])
        assert bossac._touch_1200('/dev/ttyACM0') == '/dev/ttyACM0'

    def test_touch_without_location(self, mock_serial):
        """Without a USB location, only the port's own name is accepted."""
        due = DUE[:3]
        mock_serial.set_ports([due], [], [OTHER_BOARD[:3]], [OTHER_BOARD[:3], due])
        assert bossac._touch_1200('/dev/ttyACM0') == '/dev/ttyACM0'

    def test_touch_bridge(self, mock_serial):
        """A port that never disappears - that of a USB-to-serial bridge, or of a
        bootloader that ignores the touch - is the bootloader's after settling."""
        mock_serial.set_ports([DUE_BRIDGE])
        assert bossac._touch_1200('/dev/ttyACM0') == '/dev/ttyACM0'

    @pytest.mark.skipif(sys.platform == 'win32', reason='symlinks need privileges')
    def test_touch_alias(self, mock_serial, tmp_path):
        """A port given by an alias, e.g. /dev/serial/by-id/..., is resolved."""
        (alias := tmp_path / 'usb-Arduino_Due_Prog_Port-if00').symlink_to(DUE[0])
        mock_serial.set_ports([DUE_BRIDGE])
        assert bossac._touch_1200(str(alias)) == '/dev/ttyACM0'
        mock_serial.assert_called_once_with('/dev/ttyACM0', 1200)

    def test_touch_unknown_port(self, mock_serial):
        """A port that isn't listed raises FileNotFoundError without being touched."""
        mock_serial.set_ports([DUE])
        with pytest.raises(FileNotFoundError, match='Could not find serial port'):
            bossac._touch_1200('/dev/ttyACM9')
        mock_serial.assert_not_called()

    def test_touch_timeout(self, mock_serial):
        """A port that disappears for good raises TimeoutError."""
        mock_serial.set_ports([DUE], [])
        with pytest.raises(TimeoutError, match='No bootloader appeared'):
            bossac._touch_1200('/dev/ttyACM0')


# POSIX paths can't be resolved on Windows - os.path.realpath stays Windows' own
POSIX_ONLY = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX paths')


class TestPortKey:
    @pytest.mark.parametrize(
        ('platform', 'port', 'expected'),
        [
            ('win32', 'com3', 'COM3'),
            ('win32', '\\\\.\\COM10', 'COM10'),
            pytest.param(
                'darwin',
                '/dev/tty.usbmodem1101',
                '/dev/cu.usbmodem1101',
                marks=POSIX_ONLY,
            ),
            pytest.param(
                'darwin',
                '/dev/cu.usbmodem1101',
                '/dev/cu.usbmodem1101',
                marks=POSIX_ONLY,
            ),
            pytest.param('linux', '/dev/ttyACM0', '/dev/ttyACM0', marks=POSIX_ONLY),
        ],
    )
    def test_port_key(self, platform, port, expected):
        """Aliases of a port's name are mapped to the name pyserial lists."""
        with patch('bossac.sys.platform', platform):
            assert bossac._port_key(port) == expected


class TestReadHex:
    def test_read_hex_arduino(self):
        """A HEX file built by the Arduino IDE matches the binary of the same build."""
        assert bossac._read_hex(BLINK_HEX) == (BLINK_BIN.read_bytes(), 0x2000)

    def test_read_hex(self, tmp_path):
        """Data records are assembled at their addresses, with gaps filled with 0xFF."""
        hex_file = _write_hex(
            tmp_path / 'firmware.hex',
            _hex_record(0x04, data=b'\x00\x08'),
            _hex_record(0x00, 0x0004, b'\x05\x06'),
            _hex_record(0x00, 0x0000, b'\x01\x02'),
            _hex_record(0x05, data=b'\x00\x08\x00\x00'),
            '',
            _hex_record(0x02, data=b'\x80\x00'),  # segment 0x8000 -> 0x80000
            _hex_record(0x00, 0x0008, b'\x09'),
        )
        data, start = bossac._read_hex(hex_file)
        assert start == 0x80000
        assert data == b'\x01\x02\xff\xff\x05\x06' + b'\xff' * 2 + b'\x09'

    def test_read_hex_ignores_after_eof(self, tmp_path):
        """Records after the end-of-file record are ignored."""
        hex_file = _write_hex(
            tmp_path / 'firmware.hex',
            _hex_record(0x00, 0x0000, b'\x01'),
            _hex_record(0x01),
            _hex_record(0x00, 0x0001, b'\x02'),
        )
        assert bossac._read_hex(hex_file) == (b'\x01', 0)

    @pytest.mark.parametrize(
        ('records', 'message'),
        [
            ([], 'contains no data'),
            (['01000000AA'], 'Invalid record in .*, line 1: missing start code'),
            ([_hex_record(0x06, data=b'\x00')], 'unknown record type 0x06'),
            ([_hex_record(0x04, data=b'\x00\x08\x00')], 'invalid length .* 0x04'),
            ([_hex_record(0x02, data=b'\x80')], 'invalid length .* 0x02'),
            ([_hex_record(0x05, data=b'\x00')], 'invalid length .* 0x05'),
            ([_hex_record(0x01, data=b'\x00')], 'invalid length .* 0x01'),
            ([':0100000001'], 'Invalid record'),  # too short
            ([':01000000AA00'], 'Invalid record'),  # invalid checksum
            ([':01000000ZZ00'], 'Invalid record'),  # not hex
            (
                [
                    _hex_record(0x00, 0, b'\x00'),
                    _hex_record(0x04, data=b'\x10\x00'),
                    _hex_record(0x00, 0, b'\x00'),
                ],
                'too much to be flashed',
            ),
        ],
    )
    def test_read_hex_invalid(self, tmp_path, records, message):
        """Invalid files raise ValueError."""
        hex_file = _write_hex(tmp_path / 'firmware.hex', *records)
        with pytest.raises(ValueError, match=message):
            bossac._read_hex(hex_file)


class TestVersion:
    def test_version(self):
        """The bundled binary reports the version that bossac-wrapper expects."""
        assert bossac.version() == bossac._BOSSAC_VERSION

    @pytest.mark.parametrize('output', ['', 'Usage: bossac [OPTION...] [FILE]'])
    def test_version_parsing_invalid(self, output):
        """Output without a version raises RuntimeError."""
        with (
            patch('bossac._call_bossac', return_value=output),
            pytest.raises(RuntimeError, match='Could not determine the version'),
        ):
            bossac.version()


class TestParseFirmwareFile:
    def test_parse_firmware_file(self, tmp_path):
        """Directories and missing files raise errors, and str paths are accepted."""
        with pytest.raises(IsADirectoryError) as exc_info:
            bossac._parse_firmware_file(tmp_path)
        assert exc_info.value.errno == errno.EISDIR
        assert exc_info.value.filename == str(tmp_path.resolve())

        firmware_file = tmp_path / 'firmware.bin'
        with pytest.raises(FileNotFoundError) as exc_info:
            bossac._parse_firmware_file(firmware_file)
        assert exc_info.value.errno == errno.ENOENT
        assert exc_info.value.filename == str(firmware_file.resolve())

        firmware_file.touch()
        assert (
            bossac._parse_firmware_file(str(firmware_file)) == firmware_file.resolve()
        )

    @pytest.mark.parametrize(
        'name', ['blink.bin', 'blink.BIN', 'blink.hex', 'blink.ino.bin', 'v1.2.hex']
    )
    def test_parse_firmware_file_valid_extension(self, tmp_path, name):
        """Supported extensions are accepted, regardless of case and other dots."""
        (firmware_file := tmp_path / name).touch()
        assert bossac._parse_firmware_file(firmware_file) == firmware_file.resolve()

    @pytest.mark.parametrize('name', ['blink', '.bin', 'blink.elf', 'blink.bin.bak'])
    def test_parse_firmware_file_invalid_extension(self, tmp_path, name):
        """Missing or unsupported extensions raise ValueError."""
        (firmware_file := tmp_path / name).touch()
        with pytest.raises(ValueError, match=r'unrecognized extension \(supported: '):
            bossac._parse_firmware_file(firmware_file)


class TestBossacError:
    def test_bossac_error_message(self):
        """The message is bossac's error message, without surrounding whitespace."""
        error = bossac.BossacError(1, ['bossac'], stderr='\nNo device found on COM3\n')
        assert str(error) == 'No device found on COM3'

    def test_bossac_error_message_with_hint(self):
        """A hint follows the message."""
        error = bossac.BossacError(1, ['bossac'], stderr='SAM-BA operation failed')
        error.hint = 'Try harder.'
        assert str(error) == 'SAM-BA operation failed\nTry harder.'

    def test_bossac_error_message_without_stderr(self):
        """Without an error message, the message is the exit status."""
        for stderr in (None, ''):
            error = bossac.BossacError(1, ['bossac'], stderr=stderr)
            assert str(error) == str(CalledProcessError(1, ['bossac']))


class TestCallBossac:
    def test_call_bossac(self, mock_popen):
        """Only an unexpected exit code raises BossacError, carrying bossac's output."""
        mock_popen.set_pipes(['status'], ['warning!'])
        assert bossac._call_bossac([]) == 'status'

        mock_popen.set_pipes(['status'], ['error!'])
        mock_popen.set_returncode(1)
        with pytest.raises(bossac.BossacError) as exc_info:
            bossac._call_bossac([])
        assert isinstance(exc_info.value, CalledProcessError)
        assert str(exc_info.value) == 'error!'
        assert exc_info.value.returncode == 1
        assert exc_info.value.output == 'status'
        assert exc_info.value.stderr == 'error!'

        assert bossac._call_bossac([], returncodes=(0, 1)) == 'status'

    @pytest.mark.parametrize(
        ('stderr', 'port', 'expect_hint'),
        [
            (['', 'SAM-BA operation failed'], '/dev/ttyACM0', True),
            (['', 'No device found on /dev/ttyACM0'], '/dev/ttyACM0', True),
            (['', 'SAM-BA operation failed'], None, False),  # no port given
            (['', 'Flash command failed'], '/dev/ttyACM0', False),  # device answered
        ],
    )
    def test_call_bossac_hint(self, mock_popen, stderr, port, expect_hint):
        """Failures to reach a SAM-BA bootloader on a port come with a hint."""
        mock_popen.set_pipes([], stderr)
        mock_popen.set_returncode(1)
        with pytest.raises(bossac.BossacError) as exc_info:
            bossac._call_bossac([], port=port)
        if expect_hint:
            assert 'may not be in its SAM-BA bootloader' in exc_info.value.hint
            assert str(exc_info.value).startswith(f'{stderr[1]}\n')
        else:
            assert exc_info.value.hint is None
            assert str(exc_info.value) == stderr[1]

    def test_call_bossac_logging(self, mock_popen, caplog):
        """Status is logged at log_level, progress at DEBUG, empty lines never."""
        caplog.set_level(logging.DEBUG)
        mock_popen.set_pipes(
            [
                'Write 4 bytes to flash (1 pages)',
                '',
                '[                              ] 0% (0/1 pages)',
                '[==============================] 100% (1/1 pages)',
                'Done in 0.010 seconds',
            ],
            [],
        )
        bossac._call_bossac([], log_level=logging.INFO)
        assert [(r.levelno, r.getMessage()) for r in caplog.records[1:]] == [
            (logging.INFO, 'Write 4 bytes to flash (1 pages)'),
            (logging.DEBUG, '[                              ] 0% (0/1 pages)'),
            (logging.DEBUG, '[==============================] 100% (1/1 pages)'),
            (logging.INFO, 'Done in 0.010 seconds'),
        ]

        # NOTSET disables the progress bars, too
        caplog.clear()
        bossac._call_bossac([], log_level=logging.NOTSET)
        assert [r.getMessage() for r in caplog.records[1:]] == []

    def test_call_bossac_carriage_returns(self, caplog):
        """Progress bars separated by carriage returns are logged one by one."""
        script = (
            'import sys; '
            "sys.stdout.write('Write\\n[=  ] 50% (1/2 pages)\\r[== ] 100% (2/2 pages)"
            "\\nDone\\n')"
        )
        caplog.set_level(logging.DEBUG)
        with patch('bossac._resolve_bossac', return_value=sys.executable):
            bossac._call_bossac([*PYTHON_ARGS, script], log_level=logging.INFO)
        assert [(r.levelno, r.getMessage()) for r in caplog.records[1:]] == [
            (logging.INFO, 'Write'),
            (logging.DEBUG, '[=  ] 50% (1/2 pages)'),
            (logging.DEBUG, '[== ] 100% (2/2 pages)'),
            (logging.INFO, 'Done'),
        ]

    def test_call_bossac_decoding(self):
        """Output is decoded as UTF-8, with invalid bytes replaced."""
        script = (
            'import sys; '
            "sys.stdout.buffer.write(b'caf\\xc3\\xa9 M\\xfcller\\n'); "
            "sys.stderr.buffer.write(b'caf\\xc3\\xa9 M\\xfcller\\n'); "
            'sys.exit(1)'
        )
        with (
            patch('bossac._resolve_bossac', return_value=sys.executable),
            pytest.raises(bossac.BossacError) as exc_info,
        ):
            bossac._call_bossac([*PYTHON_ARGS, script])
        assert exc_info.value.output == 'café M�ller'
        assert exc_info.value.stderr == 'café M�ller'
        assert exc_info.value.cmd == [sys.executable, *PYTHON_ARGS, script]

    def test_call_bossac_kills_bossac_on_error(self):
        """An error while reading the output kills bossac instead of waiting for it."""
        script = "import time; print('waiting', flush=True); time.sleep(60)"
        threads_before = threading.active_count()
        start = time.monotonic()
        with (
            patch('bossac._resolve_bossac', return_value=sys.executable),
            patch.object(bossac.log, 'log', side_effect=RuntimeError('handler failed')),
            pytest.raises(RuntimeError, match='handler failed'),
        ):
            bossac._call_bossac([*PYTHON_ARGS, script], log_level=logging.INFO)
        assert time.monotonic() - start < 30
        assert threading.active_count() == threads_before

    def test_call_bossac_timeout(self):
        """bossac is killed if it stops producing output, e.g. on a stale port."""
        script = "import time; print('Erase flash', flush=True); time.sleep(60)"
        threads_before = threading.active_count()
        start = time.monotonic()
        with (
            patch('bossac._resolve_bossac', return_value=sys.executable),
            patch('bossac._BOSSAC_TIMEOUT', 0.5),
            pytest.raises(TimeoutError, match='stopped responding for 0.5 s'),
        ):
            bossac._call_bossac([*PYTHON_ARGS, script])
        assert time.monotonic() - start < 30
        assert threading.active_count() == threads_before

    def test_call_bossac_no_timeout_while_busy(self):
        """Steady output keeps bossac alive, even beyond the timeout in total."""
        script = (
            'import time\n'
            'for i in range(8):\n'
            "    print(f'[==] {i}% (1/1 pages)', flush=True)\n"
            '    time.sleep(0.1)'
        )
        with (
            patch('bossac._resolve_bossac', return_value=sys.executable),
            patch('bossac._BOSSAC_TIMEOUT', 0.5),
        ):
            output = bossac._call_bossac([*PYTHON_ARGS, script])
        assert output.count('pages') == 8

    def test_call_bossac_stderr_error(self):
        """An error while reading stderr kills bossac and is raised to the caller."""
        script = (
            "import sys, time; sys.stderr.write('x' * 300_000 + '\\n'); "
            'sys.stderr.flush(); time.sleep(60)'
        )
        threads_before = threading.active_count()
        start = time.monotonic()
        with (
            patch('bossac._resolve_bossac', return_value=sys.executable),
            patch.object(bossac.log, 'log', side_effect=RuntimeError('handler failed')),
            pytest.raises(RuntimeError, match='handler failed'),
        ):
            bossac._call_bossac([*PYTHON_ARGS, script])
        assert time.monotonic() - start < 30
        assert threading.active_count() == threads_before


class TestAssembleArgs:
    def test_assemble_args(self):
        """The port's options precede the other arguments."""
        output = bossac._assemble_args(['--info'])
        assert Path(output[0]).name == bossac._BOSSAC_NAME
        assert output[1:] == ['--info']
        assert bossac._assemble_args(['--info'], port='COM3')[1:] == [
            '--port=COM3',
            '--info',
        ]
        assert bossac._assemble_args([], port='COM3', usb=True)[1:] == [
            '--port=COM3',
            '--usb-port=1',
        ]


def _record_entry(path: Path) -> SimpleNamespace:
    # stand-in for an importlib.metadata.PackagePath from the package's RECORD
    return SimpleNamespace(name=path.name, locate=lambda: path)


class TestResolveBossac:
    def test_resolve_bossac(self):
        """The bundled binary is found in the installed package's RECORD."""
        path = Path(bossac._resolve_bossac())
        assert path.is_file()
        assert path.name == bossac._BOSSAC_NAME

    def test_resolve_bossac_record(self, tmp_path):
        """The binary's RECORD entry is resolved to an absolute path."""
        binary = tmp_path / 'bin' / bossac._BOSSAC_NAME
        binary.parent.mkdir()
        binary.touch()
        record = [
            _record_entry(tmp_path / 'site-packages' / 'bossac' / '__init__.py'),
            _record_entry(binary),
        ]
        with patch('bossac.files', return_value=record):
            assert bossac._resolve_bossac() == str(binary.resolve())

    @pytest.mark.parametrize(
        'files_kwargs',
        [
            {'side_effect': bossac.PackageNotFoundError('bossac-wrapper')},
            {'return_value': None},
            {'return_value': [_record_entry(Path('bossac', '__init__.py'))]},
            {'return_value': [_record_entry(Path('does', 'not', 'exist', 'bossac'))]},
        ],
    )
    def test_resolve_bossac_not_found(self, files_kwargs):
        """A missing binary raises FileNotFoundError instead of falling back to PATH."""
        with (
            patch('bossac.files', **files_kwargs),
            patch('bossac._BOSSAC_NAME', 'bossac'),
            pytest.raises(FileNotFoundError, match='bundled with bossac-wrapper'),
        ):
            bossac._resolve_bossac()
