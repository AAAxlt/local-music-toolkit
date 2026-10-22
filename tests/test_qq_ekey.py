#!/usr/bin/env python3
"""Synthetic parser + no-Frida provider tests (no real material)."""

import ast
import base64
import contextlib
import io
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import qq_ekey as core
import qq_local_cache as cache
import qq_windows_inputs as windows
from Cryptodome.Cipher import AES

TEA32 = bytes.fromhex("41ea3a0a94baa940")
KEY = b"B24B7F5600775751CE2BF17A7DD36CFD"
PREFIXES = ["p0", "p1", "p2", "p3", "p4", "p5", "p6"]
SUFFIXES = ["s0", "s1", "s2", "s3", "s4"]
IDENT = "001ABC123DEF4567"
MACHINE = {"macs": ["aa:bb:cc:dd:ee:ff"],
           "drives": [{"index": 0, "serial": "SER12345",
                       "model": "Model", "firmware": "FW1.0"}]}


def ref_tea(block, key, rounds=16):
    mask, delta = 0xFFFFFFFF, 0x9E3779B9
    v0, v1 = int.from_bytes(block[:4], "big"), int.from_bytes(block[4:], "big")
    k = [int.from_bytes(key[i:i + 4], "big") for i in range(0, 16, 4)]
    total = 0
    for _ in range(rounds):
        total = (total + delta) & mask
        v0 = (v0 + ((((v1 << 4) + k[0]) & mask) ^ ((v1 + total) & mask)
                    ^ (((v1 >> 5) + k[1]) & mask))) & mask
        v1 = (v1 + ((((v0 << 4) + k[2]) & mask) ^ ((v0 + total) & mask)
                    ^ (((v0 >> 5) + k[3]) & mask))) & mask
    return v0.to_bytes(4, "big") + v1.to_bytes(4, "big")


def enc_chain(data, key):
    q_prev, c_prev = data[:8], core.qqtea_encrypt_block(data[:8], key)
    out = bytearray(c_prev)
    for offset in range(8, len(data), 8):
        q = bytes(a ^ b for a, b in zip(data[offset:offset + 8], c_prev))
        c = bytes(a ^ b for a, b in zip(core.qqtea_encrypt_block(q, key), q_prev))
        out += c
        q_prev, c_prev = q, c
    return bytes(out)


def body(size, pad=6, seed=0x31):
    value = bytearray(((seed + i * 29) & 0xFF) for i in range(size))
    value[0] = (value[0] & 0xF8) | pad
    return bytes(value)


def wrap(payload, pad=6):
    header = bytearray([0xA5] * (pad + 3))
    header[0] = (header[0] & 0xF8) | pad
    full = bytes(header) + payload + b"\x00" * 7
    assert len(full) % 8 == 0
    return full


def ekey_material(payload, simple=b"ABCDEFGH"):
    key = core.interleave_tan_key(core.DEFAULT_TAN_KEY, simple)
    return simple + enc_chain(wrap(payload), key)


def ekey(payload):
    return base64.b64encode(ekey_material(payload))


def synthetic_encv2(material_bytes):
    middle = base64.b64encode(material_bytes)
    layer2 = enc_chain(wrap(middle), core.STATIC_KEY_2)
    layer1 = enc_chain(wrap(layer2), core.STATIC_KEY_1)
    return base64.b64encode(core.ENC_V2_PREFIX + base64.b64encode(layer1))


def aes_encrypt(key, iv, plaintext):
    return AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128).encrypt(plaintext)


def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def fields(items):
    out = bytearray(varint(0))
    for key, value in items:
        out += varint(len(key)) + key + varint(len(value)) + value
    return bytes(out)


def write_mmkv(directory, name, ekey_value, key, version=3, crc=None, actual=None):
    iv = bytes(range(16, 32))
    cipher = aes_encrypt(key[:16], iv, fields([(IDENT.encode(), ekey_value)]))
    size = len(cipher) if actual is None else actual
    data = struct.pack("<I", size) + cipher + b"\x00" * 4
    checksum = zlib.crc32(cipher) & 0xFFFFFFFF if crc is None else crc
    meta = struct.pack("<II", checksum, version) + b"\x00" * 4 + iv + b"\x00" * 8
    path = Path(directory) / (name + ".cah")
    path.write_bytes(data)
    Path(str(path) + ".crc").write_bytes(meta)
    return path


def write_source(path):
    ext = bytearray(176)
    encoded = IDENT.encode("utf-16-le")
    ext[72:72 + len(encoded)] = encoded
    Path(path).write_bytes(b"fLaC" + b"x" * 64 + bytes(ext)
                           + struct.pack("<II", 192, 1) + b"musicex\x00")
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ParserTest(unittest.TestCase):
    def test_tea_vector_reference_and_shapes(self):
        self.assertEqual(core.qqtea_encrypt_block(bytes(8), bytes(16), rounds=32), TEA32)
        self.assertEqual(core.qqtea_decrypt_block(TEA32, bytes(16), rounds=32), bytes(8))
        key = bytes(range(16))
        for index in range(4):
            block = bytes((index * 8 + i * 7) & 0xFF for i in range(8))
            expected = ref_tea(block, key)
            self.assertEqual(core.qqtea_encrypt_block(block, key), expected)
            self.assertEqual(core.qqtea_decrypt_block(expected, key), block)
        for block, key in ((bytes(7), bytes(16)), (bytes(8), bytes(15))):
            with self.assertRaises(core.EkeyError):
                core.qqtea_decrypt_block(block, key)

    def test_static_keys_default_tan_and_wrapping(self):
        self.assertEqual(core.STATIC_KEY_1.hex(), "3338365a4a592140232a24255e262928")
        self.assertEqual(core.STATIC_KEY_2.hex(), "2a2a232128232425265e6131635a2c54")
        self.assertEqual(core.DEFAULT_TAN_KEY.hex(), "695646382b20150b")
        self.assertEqual(len(core.ENC_V2_PREFIX), 0x12)
        words = (10, 0x11111111, 0x22222222, 0x33333333,
                 0x44444444, 0x55555555, 0x66666666)
        expected = b"".join((word ^ 10).to_bytes(4, "little")
                            for word in words[2:6])
        self.assertEqual(core.decode_wrapping_words(words, 7), expected)
        self.assertEqual(core.interleave_tan_key(bytes(range(8)), bytes(range(16, 24))),
                         bytes([0, 16, 1, 17, 2, 18, 3, 19, 4, 20, 5, 21, 6, 22, 7, 23]))

    def test_strict_base64(self):
        for text in (b"AA==", b"AA", b"AAA=", b"AAA", b"AAAA"):
            self.assertIsInstance(core._strict_base64(text, "test"), bytes)
        for text in (b"", b"A", b"AA===", b"AA=A", b"AA A", b"AA\nAA",
                     b"AB==", b"\xff"):
            with self.assertRaises(core.EkeyError):
                core._strict_base64(text, "test")
        with self.assertRaises(core.EkeyError) as caught:
            core._strict_base64(b"A" * (core.MAX_EKEY_BYTES + 1), "test")
        self.assertEqual(caught.exception.code, "input-too-large")

    def test_five_sizes_512_material_and_default_tan(self):
        for size in (8, 16, 24, 32, 40, 504):
            payload = body(size, seed=size)
            with self.subTest(size=size):
                self.assertEqual(core.parse_ekey(ekey(payload)),
                                 b"ABCDEFGH" + payload)
        self.assertEqual(len(core.parse_ekey(ekey(body(504)))), 512)

    def test_encv2_synthetic_roundtrip(self):
        payload = body(16, seed=0x91)
        encoded = synthetic_encv2(ekey_material(payload))
        self.assertTrue(base64.b64decode(encoded).startswith(core.ENC_V2_PREFIX))
        self.assertEqual(core.parse_ekey(encoded), b"ABCDEFGH" + payload)

    def test_padding_failures_and_malformed(self):
        payload = body(16)
        simple = b"ABCDEFGH"
        key = core.interleave_tan_key(core.DEFAULT_TAN_KEY, simple)
        for index in range(-7, 0):
            full = bytearray(wrap(payload))
            full[index] = 1
            with self.subTest(index=index):
                with self.assertRaises(core.EkeyError) as caught:
                    core.parse_material(simple + enc_chain(bytes(full), key))
                self.assertEqual(caught.exception.code, "padding-invalid")
        plain = bytearray(16)
        plain[0] = 7
        with self.assertRaises(core.EkeyError):
            core.parse_material(simple + enc_chain(bytes(plain), key))
        for material in (b"12345678", b"12345678" + bytes(17)):
            with self.assertRaises(core.EkeyError):
                core.parse_material(material)
        with self.assertRaises(core.EkeyError):
            core._peel_encv2(core.ENC_V2_PREFIX + b"!!!!")

    def test_no_print_and_stdlib_parser(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            out = core.parse_ekey(ekey(body(16)))
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(out, b"ABCDEFGH" + body(16))
        tree = ast.parse(Path(core.__file__).read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertLessEqual(imports, {"__future__", "base64", "struct"})


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.ekey = ekey(body(504))
        self.material = core.parse_ekey(self.ekey)
        self.source = self.root / "song.mflac"
        self.sha = write_source(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def provider(self, **kwargs):
        kwargs.setdefault("cache_root", self.snapshot)
        kwargs.setdefault("machine_inputs", MACHINE)
        kwargs.setdefault("filename_parts", (PREFIXES, SUFFIXES))
        return cache.LocalCacheMaterialProvider(**kwargs)

    def test_candidates_and_filename(self):
        keys = list(cache.iter_key_candidates(MACHINE))
        self.assertIn(KEY, keys)
        self.assertEqual(cache.derive_cache_filename(KEY, PREFIXES, SUFFIXES),
                         "p3bbfcs2")
        variants = cache.hardware_variants(MACHINE)
        self.assertEqual(variants[1][0], "SER12345")
        self.assertEqual(variants[2][0], "ES1R3254")
        self.assertTrue(all(x == x.strip() for item in variants for x in item))
        self.assertEqual(windows.normalize_mac("AA:BB:CC:DD:EE:FF"),
                         "aabbccddeeff")
        if os.name != "nt":
            self.assertEqual(windows.read_machine_inputs(),
                             {"macs": [], "drives": []})

    def test_fields_strict(self):
        self.assertEqual(cache.parse_mmkv_fields(fields([(b"s", b"e")])),
                         {b"s": b"e"})
        self.assertEqual(cache.parse_mmkv_fields(varint(0xffffffff)+fields([(b"s",b"e")])[1:]),{b"s":b"e"})
        self.assertEqual(cache.parse_mmkv_fields(fields([(b"s", b"e"), (b"s", b""), (b"k", b"v")])), {b"k": b"v"})
        good = fields([(b"s", b"e")])
        for bad in (good[:-1], good + b"\x00", b"\x80", b"\x80\x00\x01a\x01b"):
            with self.assertRaises(cache.CacheError):
                cache.parse_mmkv_fields(bad)

    def test_source_tail(self):
        self.assertEqual(cache.source_identifier(self.source), IDENT)
        self.assertEqual(cache.source_sha256(self.source), self.sha)
        bad = self.root / "bad"
        bad.write_bytes(b"x" * 192)
        with self.assertRaises(cache.CacheError):
            cache.source_identifier(bad)

    def test_fetch_and_privacy(self):
        write_mmkv(self.snapshot, "p3bbfcs2", self.ekey, KEY)
        provider = self.provider()
        before = sorted(p.name for p in self.snapshot.iterdir())
        self.assertEqual(provider.fetch([self.source]), {self.sha: self.material})
        self.assertEqual(provider.fetch(self.source), {self.sha: self.material})
        self.assertEqual(sorted(p.name for p in self.snapshot.iterdir()), before)
        self.assertEqual(len(self.material), 512)
        text = repr(provider.diagnostics)
        for secret in (self.ekey.decode(), self.material.hex(), IDENT):
            self.assertNotIn(secret, text)

    def test_default_tables_reject_unknown_filename(self):
        write_mmkv(self.snapshot, "xxbbfcyy", self.ekey, KEY)
        self.assertEqual(self.provider(filename_parts=None).fetch([self.source]),
                         {})

    def test_default_tables_all_five_suffixes(self):
        for suffix in cache.SUFFIXES:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as folder:
                for number in range(1,128):
                    machine={'macs':['%012x'%number], 'drives':MACHINE['drives']}
                    matches=[k for k in cache.iter_key_candidates(machine) if cache.derive_cache_filename(k,cache.PREFIXES,cache.SUFFIXES).endswith(suffix)]
                    if matches:break
                self.assertTrue(matches)
                key=matches[-1];source=write_mmkv(folder,'unused',self.ekey,key)
                target=Path(folder)/cache.derive_cache_filename(key,cache.PREFIXES,cache.SUFFIXES)
                source.rename(target);Path(str(source)+'.crc').rename(Path(str(target)+'.crc'))
                self.assertEqual(cache.LocalCacheMaterialProvider(cache_root=folder,machine_inputs=machine).fetch([self.source]),{self.sha:self.material})


    def test_non_512_material_and_variable_musicex_tag(self):
        ekey_value = ekey(body(248))
        material = core.parse_ekey(ekey_value)
        self.assertEqual(len(material), 256)
        tag = bytearray(256)
        encoded = IDENT.encode("utf-16-le")
        tag[0x48:0x48 + len(encoded)] = encoded
        tag[-16:-12] = struct.pack("<I", 256)
        tag[-12:-8] = struct.pack("<I", 1)
        tag[-8:] = b"musicex\x00"
        source = self.root / "varied.mflac"
        source.write_bytes(b"fLaC" + b"x" * 64 + bytes(tag))
        self.assertEqual(cache.source_identifier(source), IDENT)
        write_mmkv(self.snapshot, "p3bbfcs2", ekey_value, KEY)
        sha = cache.source_sha256(source)
        got = self.provider().fetch([source])
        self.assertEqual(got, {sha: material})
        self.assertIn(len(material), cache.SUPPORTED_MATERIAL_LENGTHS)

    def test_unsupported_material_length_is_not_guessed(self):
        write_mmkv(self.snapshot, "p3bbfcs2", self.ekey, KEY)
        with patch.object(cache.qq_ekey, "parse_ekey", return_value=b"x" * 500):
            self.assertEqual(self.provider().fetch([self.source]), {})

    @unittest.skipUnless(os.name=='nt','Windows API structure regression')
    def test_getadaptersinfo_buffer_overflow_is_size_query(self):
        import ctypes
        from ctypes import wintypes
        from types import SimpleNamespace
        from unittest.mock import Mock
        def api(buffer,size):
            if buffer is None:size._obj.value=512;return 111
            raw=bytearray(512);struct.pack_into('<I',raw,404,6);raw[408:414]=bytes.fromhex('aabbccddeeff')
            ctypes.memmove(buffer,bytes(raw),512);return 0
        fake=SimpleNamespace(GetAdaptersInfo=Mock(side_effect=api))
        with patch.object(ctypes,'WinDLL',return_value=fake):
            self.assertEqual(windows._macs_from_adapters(ctypes,wintypes),{'aabbccddeeff'})

    def test_bad_crc_version_truncation_limit_deletion(self):
        cases = {"crc": dict(crc=0), "v1": dict(version=1),
                 "v5": dict(version=5),
                 "limit": dict(actual=cache.MAX_CACHE_BYTES + 1)}
        for label, options in cases.items():
            with self.subTest(label=label):
                path = write_mmkv(self.snapshot, "p3bbfcs2", self.ekey, KEY, **options)
                self.assertEqual(self.provider().fetch([self.source]), {})
                path.unlink()
                Path(str(path) + ".crc").unlink()
        path = write_mmkv(self.snapshot, "p3bbfcs2", self.ekey, KEY)
        path.write_bytes(path.read_bytes()[:3])
        self.assertEqual(self.provider().fetch([self.source]), {})
        write_mmkv(self.snapshot, "p3bbfcs2", self.ekey, bytes(32))
        self.assertEqual(self.provider().fetch([self.source]), {})

    def test_lazy_os_and_no_writes(self):
        provider = self.provider(machine_inputs=None)
        with patch.object(cache, "read_machine_inputs",
                          side_effect=AssertionError("eager OS read")):
            self.assertEqual(provider.fetch([self.source]), {})
            self.assertEqual(provider.fetch([]), {})
        write_mmkv(self.snapshot, "p3bbfcs2", self.ekey, KEY)
        provider = self.provider(machine_inputs=None)
        with patch.object(cache, "read_machine_inputs", return_value=MACHINE) as read:
            self.assertEqual(provider.fetch([self.source]),
                             {self.sha: self.material})
            self.assertEqual(read.call_count, 1)

    def test_module_markers(self):
        source = Path(cache.__file__).read_text(encoding="utf-8")
        for marker in ("import frida", "import psutil", "import requests",
                       "import socket", "import subprocess", "LoadLibrary",
                       "OpenProcess", "CreateRemoteThread"):
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
