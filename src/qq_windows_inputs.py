"""Read-only current-user Windows machine inputs for MMKV key derivation.

Raw MACs and disk descriptors are returned in memory only.  This module never
writes them to disk, never imports the QQ client, never attaches to a process
and never performs network I/O.  On non-Windows hosts it returns empty inputs
without loading any DLL, so ordinary unit tests stay hermetic.

The disk collector follows the calibrated probe field order: serial from
descriptor offset 24, model from offset 16, firmware from offset 20.  Only
printable ASCII is retained.  A failed OS read simply removes that candidate;
empty candidate lists are valid.
"""

from __future__ import annotations

import os
import re

__all__ = ["read_machine_inputs", "normalize_mac"]

_MAC_HEX = re.compile(r"[0-9a-f]{12}")
_MAX_DRIVES = 32
_MAX_FIELD = 256


def normalize_mac(value):
    """Return a lowercase 12-hex MAC or '' for unusable input."""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[^0-9A-Fa-f]", "", value).lower()
    if len(text) != 12 or _MAC_HEX.fullmatch(text) is None:
        return ""
    if int(text, 16) == 0:
        return ""
    return text


def _printable(value, limit=_MAX_FIELD):
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > limit:
        return ""
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in text):
        return ""
    return text


def _macs_from_netwksta(ctypes, wintypes):
    macs = set()

    class Transport(ctypes.Structure):
        _fields_ = [
            ("quality", wintypes.DWORD),
            ("vcs", wintypes.DWORD),
            ("name", wintypes.LPWSTR),
            ("address", wintypes.LPWSTR),
            ("wan", wintypes.DWORD),
        ]

    net = ctypes.WinDLL("netapi32")
    buffer = ctypes.c_void_p()
    entries = wintypes.DWORD()
    total = wintypes.DWORD()
    net.NetWkstaTransportEnum.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    net.NetWkstaTransportEnum.restype = wintypes.DWORD
    net.NetApiBufferFree.argtypes = [ctypes.c_void_p]
    net.NetApiBufferFree.restype = wintypes.DWORD
    if net.NetWkstaTransportEnum(
            None, 0, ctypes.byref(buffer), 0xFFFFFFFF,
            ctypes.byref(entries), ctypes.byref(total), None) != 0:
        return macs
    try:
        if not buffer.value:
            return macs
        for item in ctypes.cast(buffer, ctypes.POINTER(Transport))[:entries.value]:
            mac = normalize_mac(item.address or "")
            if mac:
                macs.add(mac)
    finally:
        net.NetApiBufferFree(buffer)
    return macs


def _macs_from_adapters(ctypes, wintypes):
    macs = set()

    class Adapter(ctypes.Structure):
        _fields_ = [
            ("next", ctypes.c_void_p),
            ("combo", wintypes.DWORD),
            ("name", ctypes.c_char * 260),
            ("description", ctypes.c_char * 132),
            ("length", wintypes.UINT),
            ("address", ctypes.c_ubyte * 8),
            ("index", wintypes.DWORD),
            ("type", wintypes.UINT),
        ]

    ip = ctypes.WinDLL("iphlpapi")
    size = wintypes.ULONG()
    ip.GetAdaptersInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG)]
    ip.GetAdaptersInfo.restype = wintypes.ULONG
    if ip.GetAdaptersInfo(None, ctypes.byref(size)) not in (0, 111) or not 0 < size.value <= 1024*1024:
        return macs
    storage = ctypes.create_string_buffer(size.value)
    if ip.GetAdaptersInfo(storage, ctypes.byref(size)) != 0:
        return macs
    pointer = ctypes.addressof(storage)
    seen = 0
    while pointer and seen < 64:
        seen += 1
        item = Adapter.from_address(pointer)
        if item.length == 6:
            mac = bytes(item.address[:6]).hex()
            if normalize_mac(mac):
                macs.add(mac)
        pointer = item.next
    return macs


def _drives(ctypes, wintypes):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = ctypes.c_void_p(-1).value
    drives = []
    for index in range(8):
        handle = kernel.CreateFileW(
            "\\\\.\\PhysicalDrive%d" % index, 0, 3, None, 3, 0, None)
        if not handle or handle == invalid:
            continue
        try:
            query = ctypes.create_string_buffer(bytes(12))
            out = ctypes.create_string_buffer(4096)
            returned = wintypes.DWORD()
            if not kernel.DeviceIoControl(
                    handle, 0x2D1400, query, 12, out, 4096,
                    ctypes.byref(returned), None):
                continue
            raw = out.raw[:returned.value]

            def field(offset):
                if offset + 4 > len(raw):
                    return ""
                position = int.from_bytes(raw[offset:offset + 4], "little")
                if not 0 < position < len(raw):
                    return ""
                value = raw[position:].split(b"\0", 1)[0]
                try:
                    return _printable(value.decode("ascii"))
                except UnicodeDecodeError:
                    return ""

            drives.append({
                "index": index,
                "serial": field(24),
                "model": field(16),
                "firmware": field(20),
            })
            if len(drives) >= _MAX_DRIVES:
                break
        finally:
            kernel.CloseHandle(handle)
    return drives


def read_machine_inputs():
    """Return ``{'macs': [...], 'drives': [...]}`` in memory only.

    Never raises for OS failures and never writes or prints identifiers.  On a
    non-Windows host this is a constant empty result with no DLL loading.
    """
    if os.name != "nt":
        return {"macs": [], "drives": []}
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return {"macs": [], "drives": []}
    macs = set()
    for collector in (_macs_from_netwksta, _macs_from_adapters):
        try:
            macs.update(collector(ctypes, wintypes))
        except Exception:
            pass
    try:
        drives = _drives(ctypes, wintypes)
    except Exception:
        drives = []
    return {"macs": sorted(macs), "drives": drives}
