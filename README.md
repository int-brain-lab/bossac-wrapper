bossac-wrapper
==============

A thin Python wrapper for [bossac](https://github.com/shumatech/BOSSA) by
[ShumaTech](http://www.shumatech.com) - a flash programmer for Atmel SAM microcontrollers with
a SAM-BA bootloader, such as the Arduino Due, Arduino Zero and MKR boards.

Installation
------------

```shell
pip install bossac-wrapper
```

This will install bossac-wrapper along with the necessary bossac binary for your system.

bossac-wrapper requires Python 3.10 or later. Supported platforms are:

- Linux (x86_64, arm64)
- macOS (Intel, Apple Silicon)
- Windows (x86_64)

Examples
--------

### Finding the port of a device

All functions take the serial port of the device. To find it, use
[pyserial](https://pyserial.readthedocs.io/)'s `comports()`, which bossac-wrapper installs as a
dependency:

```python
from serial.tools.list_ports import comports

for port in comports():
    print(port.device, port.description, port.manufacturer)
```

```
/dev/ttyACM0 SFE SAMD21 SparkFun
```

### Uploading a firmware file

To upload a firmware file to a device, use the `upload()` function. Both Intel HEX files (`.hex`)
and raw binaries (`.bin`) are supported.

```python
import bossac
import logging

logging.basicConfig(level=logging.INFO)
bossac.upload('blink.hex', port='/dev/ttyACM0')
```

By default, the device is first put into its bootloader with a 1200 baud touch. Its flash is
then erased, written and verified, and the device is reset to start the new firmware. The
progress will be logged:
```
INFO:bossac:Erase flash
INFO:bossac:Done in 0.887 seconds
INFO:bossac:Write 10288 bytes to flash (161 pages)
INFO:bossac:Done in 0.067 seconds
INFO:bossac:Verify 10288 bytes of flash
INFO:bossac:Verify successful
INFO:bossac:Done in 0.276 seconds
INFO:bossac:Set boot flash true
```

An Intel HEX file carries its addresses, so it is flashed at the right offset on any board. A
binary file doesn't, so its offset has to be given - it depends on the board's bootloader, e.g.
`0x2000` on SAMD21 boards with an Arduino-style bootloader, or 0 on the Arduino Due, whose
bootloader is in ROM. On a board whose bootloader is in flash, an offset of 0 would overwrite it.

```python
bossac.upload('blink.bin', port='/dev/ttyACM0', offset=0x2000)  # SAMD21
```

When uploading via a USB-to-serial bridge, such as the programming port of the Arduino Due, pass
`usb=False`:

```python
bossac.upload('blink.bin', port='/dev/ttyACM0', offset=0, usb=False)
```

### Getting device information

To get the details of a device, use the `info()` function. The device must be in its bootloader,
e.g. after double-tapping its reset button. `touch=True` puts it there, too - but that usually
destroys the firmware, which then has to be uploaded again: the Arduino Due erases its flash, and
SAMD boards running Arduino firmware erase its first flash row.

```python
import bossac

bossac.info(port='/dev/ttyACM0')
```

For a SparkFun SAMD21 Mini, this returns:
```python
{
    'device': 'ATSAMD21x18',
    'version': 'v1.1 [Arduino:XYZ] Feb 20 2019 10:43:51',
    'address': 0,
    'pages': 4096,
    'page_size': 64,
    'total_size': 262144,
    'planes': 1,
    'lock_regions': 16,
    'locked': [],
    'security': False,
    'boot_flash': None,
    'brownout_detection': True,
    'brownout_reset': True,
}
```

Full Documentation
------------------

The full API documentation is available [here](https://int-brain-lab.github.io/bossac-wrapper).

![License](https://img.shields.io/github/license/int-brain-lab/bossac-wrapper)
[![Coverage](https://img.shields.io/coverallsCoverage/github/int-brain-lab/bossac-wrapper)](https://coveralls.io/github/int-brain-lab/bossac-wrapper)
[![CI](https://github.com/int-brain-lab/bossac-wrapper/actions/workflows/testing.yaml/badge.svg)](https://github.com/int-brain-lab/bossac-wrapper/actions)
[![PyPI](https://img.shields.io/pypi/v/bossac-wrapper)](https://pypi.org/project/bossac-wrapper/)
