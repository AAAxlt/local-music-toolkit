#!/usr/bin/env python3
"""Small offline-first MFLAC core: musicex v1 + verified 512-byte material.

No Frida, no client, no network.  Sources stay read-only; material bytes never
appear in the report and are cached only after a strict decode succeeds.

Private material cache policy
-----------------------------
New writes use one versioned, self-contained JSON record in the private cache
directory: ``<sourceSHA>.material-v2.json``.  The record carries the 512-byte
material as base64 (this is a private local cache, not a public report) and is
published with a same-directory temporary file, fsync, and an exclusive
``os.link``; there is no split metadata/material publication that can leave a
half-written committed entry.

Existing ``<sourceSHA>.json`` + ``<sourceSHA>.bin`` pairs are still read and
must validate exactly as before.  They are never migrated, rewritten, or
deleted.  A complete legacy pair that is malformed is cache-corrupt and does
not fall back to a live provider.

A legacy lone ``.bin`` (no ``.json``) is the signature of an interrupted old
split publication.  It is preserved byte-for-byte and used only as an
unverified 512-byte recovery candidate.  A successful strict decode publishes
the complete versioned record in the separate ``.material-v2.json`` namespace;
a candidate that fails decode is rejected without provider fallback.  A lone
``.bin`` that is not exactly 512 bytes, and a lone legacy ``.json``, are
incomplete legacy records and are reported as cache-corrupt.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qq_mflac_crypto import decrypt

ALGORITHM = "mflac-musicex-v1-offline-material-512"
TAIL_BYTES = 192
MATERIAL_BYTES = 512
MAX_FILE_BYTES = 256 * 1024 * 1024
MAGIC = b"musicex\0"
AUDIO_SUFFIXES = (".mflac", ".mflac0.flac")
WAIT_HINT = "no verified 512-byte material: populate the cache or supply a provider"

RECORD_SCHEMA = "qq-mflac-private-material"
RECORD_VERSION = 2
RECORD_SUFFIX = ".material-v2.json"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class MflacError(Exception):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category
        self.message = str(message)


def _sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise MflacError("input-invalid", "not a regular non-symlink file: %s" % path)
    return st


def _inside(child, parent):
    child, parent = os.path.normcase(os.path.realpath(child)), os.path.normcase(os.path.realpath(parent))
    return child == parent or child.startswith(parent + os.sep)


def _tail_parts(tail):
    if len(tail) != TAIL_BYTES:
        raise MflacError("unsupported", "too short for a musicex v1 tail")
    span = int.from_bytes(tail[-16:-12], "little")
    version = int.from_bytes(tail[-12:-8], "little")
    if span != TAIL_BYTES or version != 1 or tail[-8:] != MAGIC:
        raise MflacError("unsupported", "tail is not musicex v1 span=192")
    ext = tail[:-16]
    if len(ext) != 176:
        raise MflacError("unsupported", "musicex extension is not 176 bytes")
    return ext[72:172]


def _tail(data):
    if len(data) <= TAIL_BYTES:
        raise MflacError("unsupported", "too short for a musicex v1 tail")
    return data[:-TAIL_BYTES], _tail_parts(data[-TAIL_BYTES:])


def _preflight(path):
    """Stream the source hash and read only the final 192-byte tail.

    The 256 MiB bound is checked before hashing.  The payload is streamed for
    hashing, not retained here; offline decoding loads the full payload later.
    """
    before = _regular(path)
    if before.st_size > MAX_FILE_BYTES:
        raise MflacError("too-large", "source exceeds 256 MiB")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or opened.st_size != before.st_size):
            raise MflacError("source-changed", "source changed before preflight")
        size = opened.st_size
        if size <= TAIL_BYTES:
            raise MflacError("unsupported", "too short for a musicex v1 tail")
        remaining = size
        while remaining:
            chunk = handle.read(min(1 << 20, remaining))
            if not chunk:
                raise MflacError("source-changed", "source shrank during preflight")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.fstat(handle.fileno()).st_size != size:
            raise MflacError("source-changed", "source changed during preflight")
        handle.seek(size - TAIL_BYTES)
        tail = handle.read(TAIL_BYTES)
        if len(tail) != TAIL_BYTES:
            raise MflacError("source-changed", "source tail is unavailable")
        _tail_parts(tail)
    return digest.hexdigest()


def _legacy_paths(cache_dir, source_sha):
    return Path(cache_dir) / (source_sha + ".json"), Path(cache_dir) / (source_sha + ".bin")


def _cache_paths(cache_dir, source_sha):
    """Compatibility name for the legacy paired-cache paths."""
    return _legacy_paths(cache_dir, source_sha)


def _record_path(cache_dir, source_sha):
    return Path(cache_dir) / (source_sha + RECORD_SUFFIX)


def _sha_text(value, field):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("%s is not a lowercase SHA-256" % field)


def _json_object(path, label):
    try:
        text = path.read_text("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("%s is unreadable" % label) from exc
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError("%s is not valid JSON" % label) from exc
    if not isinstance(data, dict):
        raise ValueError("%s is not an object" % label)
    return data


def _v2_entry(meta_path, source_sha):
    meta = _json_object(meta_path, "cache record")
    if meta.get("schema") != RECORD_SCHEMA:
        raise ValueError("cache record schema is not supported")
    if type(meta.get("version")) is not int or meta["version"] != RECORD_VERSION:
        raise ValueError("cache record version is not supported")
    if meta.get("algorithm") != ALGORITHM:
        raise ValueError("cache record algorithm is not supported")
    if not isinstance(meta.get("sourceSHA"), str) or meta["sourceSHA"] != source_sha:
        raise ValueError("cache record does not bind this source")
    length = meta.get("materialLength")
    if type(length) is not int or length != MATERIAL_BYTES:
        raise ValueError("cache record material length is not 512")
    material_sha = meta.get("materialSHA")
    _sha_text(material_sha, "materialSHA")
    output_sha = meta.get("outputSHA")
    _sha_text(output_sha, "outputSHA")
    encoded = meta.get("materialBase64")
    if not isinstance(encoded, str):
        raise ValueError("cache record material encoding has an unexpected type")
    try:
        material = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("cache record material encoding is invalid") from exc
    if len(material) != MATERIAL_BYTES or len(material) != length:
        raise ValueError("cache record material is not 512 bytes")
    if hashlib.sha256(material).hexdigest() != material_sha:
        raise ValueError("cache record material SHA mismatch")
    return {"material": material, "outputSHA": output_sha, "kind": "v2"}


def _legacy_entry(meta_path, bin_path, source_sha):
    meta = _json_object(meta_path, "legacy cache metadata")
    if meta.get("algorithm") != ALGORITHM:
        raise ValueError("legacy cache algorithm is not supported")
    if not isinstance(meta.get("sourceSHA"), str) or meta["sourceSHA"] != source_sha:
        raise ValueError("legacy cache does not bind this source")
    length = meta.get("materialLength")
    if type(length) is not int or length != MATERIAL_BYTES:
        raise ValueError("legacy cache material length is not 512")
    material_sha = meta.get("materialSHA")
    _sha_text(material_sha, "legacy materialSHA")
    output_sha = meta.get("outputSHA")
    _sha_text(output_sha, "legacy outputSHA")
    st = _regular(bin_path)
    if st.st_size != MATERIAL_BYTES:
        raise ValueError("legacy cache material is not 512 bytes")
    blob = bin_path.read_bytes()
    if len(blob) != MATERIAL_BYTES or len(blob) != length:
        raise ValueError("legacy cache material is not 512 bytes")
    if hashlib.sha256(blob).hexdigest() != material_sha:
        raise ValueError("legacy material SHA mismatch")
    return {"material": blob, "outputSHA": output_sha, "kind": "legacy"}


def _cache_lookup(cache_dir, source_sha):
    record_path = _record_path(cache_dir, source_sha)
    meta_path, bin_path = _legacy_paths(cache_dir, source_sha)
    try:
        if os.path.lexists(record_path):
            _regular(record_path)
            return _v2_entry(record_path, source_sha)
        meta_exists = os.path.lexists(meta_path)
        bin_exists = os.path.lexists(bin_path)
        if meta_exists and bin_exists:
            return _legacy_entry(meta_path, bin_path, source_sha)
        if meta_exists:
            # A complete-looking legacy record without material is corrupt;
            # do not replace it and do not fall back to a provider.
            raise ValueError("legacy cache metadata has no material")
        if bin_exists:
            # Interrupted old split publication: preserve the .bin, but it is
            # not a complete record.  A full 512-byte blob is usable only as an
            # unverified recovery candidate; strict decode must succeed before
            # a complete v2 record is published.  Never auto-fallback after a
            # bad candidate.
            st = _regular(bin_path)
            if st.st_size != MATERIAL_BYTES:
                raise ValueError("legacy lone material is not 512 bytes")
            blob = bin_path.read_bytes()
            if len(blob) != MATERIAL_BYTES:
                raise ValueError("legacy lone material is not 512 bytes")
            return {"material": blob, "outputSHA": None, "kind": "legacy-lone"}
        return None
    except MflacError as exc:
        raise MflacError("cache-corrupt", "cache material is invalid: %s" % exc.message) from exc
    except ValueError as exc:
        raise MflacError("cache-corrupt", "cache material is invalid: %s" % exc) from exc
    except Exception as exc:
        raise MflacError("cache-corrupt", "cache material is invalid: unreadable cache data") from exc


def _cache_load(cache_dir, source_sha):
    """Return cached material bytes (original private helper contract)."""
    entry = _cache_lookup(cache_dir, source_sha)
    return None if entry is None else entry["material"]


def _fsync_dir(path):
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


def _cache_write(cache_dir, source_sha, material, output_sha):
    record_path = _record_path(cache_dir, source_sha)
    if os.path.lexists(record_path):
        return
    meta_path, bin_path = _legacy_paths(cache_dir, source_sha)
    if os.path.lexists(meta_path) and os.path.lexists(bin_path):
        # A complete legacy pair is an existing private entry: no migration.
        return
    if os.path.lexists(meta_path):
        raise OSError("legacy cache metadata is incomplete")
    meta = {
        "schema": RECORD_SCHEMA,
        "version": RECORD_VERSION,
        "algorithm": ALGORITHM,
        "sourceSHA": source_sha,
        "materialLength": len(material),
        "materialSHA": hashlib.sha256(material).hexdigest(),
        "materialBase64": base64.b64encode(material).decode("ascii"),
        "outputSHA": output_sha,
    }
    payload = json.dumps(meta, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".material-v2-", suffix=".tmp", dir=str(cache_dir))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, record_path)
        except FileExistsError:
            return
        _fsync_dir(cache_dir)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _cache_commit(cache_dir, source_sha, material, output_sha):
    try:
        _cache_write(cache_dir, source_sha, material, output_sha)
    except OSError as exc:
        return str(exc)[:200]
    return None


FLAC_HEADER_BYTES = 42
STREAMINFO_BYTES = 34
PCM_CODECS = {8: "pcm_s8", 16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}
_MD5_STDOUT = re.compile(r"MD5=([0-9a-f]{32})(?:\r?\n)?")


def _flac_streaminfo(path):
    """Read and validate the first FLAC metadata block (STREAMINFO)."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(FLAC_HEADER_BYTES)
    except OSError as exc:
        raise MflacError("audio-invalid", "FLAC header is unreadable") from exc
    if len(header) != FLAC_HEADER_BYTES or header[:4] != b"fLaC":
        raise MflacError("audio-invalid", "FLAC marker or 42-byte header is missing")
    block = header[4:8]
    if (block[0] & 0x7f) != 0 or int.from_bytes(block[1:4], "big") != STREAMINFO_BYTES:
        raise MflacError("audio-invalid", "first FLAC block is not STREAMINFO length 34")
    info = header[8:42]
    sample_rate = (info[10] << 12) | (info[11] << 4) | (info[12] >> 4)
    channels = ((info[12] >> 1) & 0x07) + 1
    bits = (((info[12] & 0x01) << 4) | (info[13] >> 4)) + 1
    total_samples = ((info[13] & 0x0f) << 32) | int.from_bytes(info[14:18], "big")
    md5 = info[18:34]
    if sample_rate == 0 or total_samples == 0 or md5 == b"\0" * 16:
        raise MflacError("audio-invalid", "FLAC STREAMINFO has zero sample count/rate/MD5")
    if bits not in PCM_CODECS:
        raise MflacError("audio-invalid", "FLAC bit depth is unsupported for PCM proof")
    return {"sample_rate": sample_rate, "channels": channels, "bits": bits,
            "total_samples": total_samples, "md5": md5.hex()}


def _strict_audio(ffmpeg, path):
    """Strict decode and full native-bit-depth signed-LE PCM MD5 proof."""
    info = _flac_streaminfo(path)
    cmd = [str(ffmpeg), "-nostdin", "-v", "error", "-xerror", "-err_detect",
           "explode", "-i", str(path), "-map", "0:a:0", "-c:a", PCM_CODECS[info["bits"]],
           "-f", "hash", "-hash", "md5", "-"]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MflacError("ffmpeg-error", "FFmpeg could not run: %s" % exc)
    if proc.returncode != 0 or proc.stderr != b"":
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise MflacError("audio-invalid", "strict FFmpeg rejected candidate: %s"
                         % (detail or "exit %d" % proc.returncode))
    try:
        text = proc.stdout.decode("ascii")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise MflacError("audio-invalid", "strict FFmpeg PCM MD5 output is not ASCII") from exc
    match = _MD5_STDOUT.fullmatch(text)
    if match is None:
        raise MflacError("audio-invalid", "strict FFmpeg did not return one PCM MD5")
    if match.group(1) != info["md5"]:
        raise MflacError("audio-invalid", "strict full-PCM MD5 mismatch")


def _tail_repair(helper, ffmpeg, candidate, work_dir):
    before, before_size = _sha(candidate), os.path.getsize(candidate)
    try:
        result = helper(ffmpeg, candidate, work_dir)
    except Exception as exc:
        raise MflacError("tail-unproven", "tail helper failed: %s" % exc)
    if not isinstance(result,tuple) or len(result)!=2:
        raise MflacError("tail-unproven","expected existing proof helper interface")
    result,removed=result
    if not isinstance(removed,int) or isinstance(removed,bool) or removed<=0 or not _inside(result,work_dir):
        raise MflacError("tail-unproven","invalid proof output/removal")
    _regular(result)
    if Path(result)==Path(candidate) or os.path.getsize(result)!=before_size-removed:
        raise MflacError("tail-unproven","proof output size mismatch")
    with open(result,"rb") as src,open(candidate,"wb") as dst:shutil.copyfileobj(src,dst)
    if _sha(candidate) == before:
        raise MflacError("tail-unproven", "tail helper made no provable change")
    with open(candidate, "rb") as handle:
        marker = handle.read(4)
    if marker != b"fLaC":
        raise MflacError("tail-unproven", "tail helper did not leave a FLAC header")
    return removed if removed > 0 else before_size - os.path.getsize(candidate)


def _publish(temp_path, target):
    if os.path.lexists(target):
        raise MflacError("output-exists", "output already exists: %s" % target)
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    try:
        os.link(temp_path, target)
    except FileExistsError:
        raise MflacError("output-exists", "output already exists: %s" % target)
    return _sha(target)


def _decrypt_candidate(payload, material):
    if not isinstance(material, (bytes, bytearray)) or len(material) != MATERIAL_BYTES:
        raise MflacError("material-invalid", "material is not exactly 512 bytes")
    try:
        candidate = decrypt(payload, bytes(material))
    except (ArithmeticError, ValueError, IndexError) as exc:
        raise MflacError("material-invalid", "material arithmetic/shape is unsupported") from exc
    if not candidate.startswith(b"fLaC"):
        raise MflacError("audio-invalid", "wrong key/material or unsupported payload")
    return candidate


def _validate_file(ffmpeg, path, tail_helper, work_root):
    """Apply the same proof to new outputs and interrupted-output recovery."""
    try:
        _strict_audio(ffmpeg, path)
    except MflacError as exc:
        if exc.category != "audio-invalid" or tail_helper is None:
            raise
        with tempfile.TemporaryDirectory(prefix='.proof-', dir=str(work_root)) as proof:
            removed = _tail_repair(tail_helper, ffmpeg, path, Path(proof))
        _strict_audio(ffmpeg, path)
        if removed <= 0:
            raise MflacError("tail-unproven", "tail helper removed no bytes")


def _process(src, rel, out_root, cache_root, ffmpeg, tail_helper, material, source_sha,
             cache_entry):
    if _regular(src).st_size > MAX_FILE_BYTES:
        raise MflacError("too-large", "source exceeds 256 MiB")
    data = src.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise MflacError("too-large", "source exceeds 256 MiB")
    if hashlib.sha256(data).hexdigest() != source_sha:
        raise MflacError("source-changed", "source changed after the cache lookup")
    payload, _embedded = _tail(data)
    if material is None:
        raise MflacError("waiting", WAIT_HINT)
    if not isinstance(material, (bytes, bytearray)) or len(material) != MATERIAL_BYTES:
        raise MflacError("material-invalid", "material is not exactly 512 bytes")
    material = bytes(material)
    out_rel = rel.with_name(re.sub(r'\.mflac\d*(?:\.flac)?$', '.flac',rel.name,flags=re.I))
    target = out_root / out_rel
    if not _inside(target, out_root):
        raise MflacError("output-escape", "output path escaped the output root")
    if os.path.lexists(target):
        try:
            _regular(target)
        except MflacError as exc:
            raise MflacError("output-exists", "output already exists: %s" % target) from exc
        if cache_entry is not None and cache_entry.get("outputSHA"):
            expected = cache_entry["outputSHA"]
            if _sha(target) == expected:
                # A hash match is not enough: old cached half-track outputs
                # must pass the same strict full-PCM proof before being reused.
                _strict_audio(ffmpeg, target)
                if _sha(src) != source_sha:
                    raise MflacError("source-changed", "source changed during validation")
                return {"source":str(rel),"sourceSHA":source_sha,"status":"unchanged",
                        "output":str(out_rel),"outputSHA":expected}
            raise MflacError("output-exists", "output already exists: %s" % target)
        # Recovery from an interruption after output publication but before the
        # cache record: accept the existing file only when it is byte-identical
        # to a freshly validated candidate, then commit the missing record.
        try:
            candidate = _decrypt_candidate(payload, material)
        except MflacError as exc:
            if cache_entry is None:
                raise MflacError("output-exists",
                                 "output already exists: %s" % target) from exc
            raise
        # The previously published output may already have its proven FLAC
        # trailer removed. Compare it with the same normalized, verified
        # candidate; never modify the existing output to make a match.
        fd, temp_name = tempfile.mkstemp(prefix='.recover-', suffix='.part', dir=str(out_root))
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(candidate)
            _validate_file(ffmpeg, Path(temp_name), tail_helper, out_root)
            expected = _sha(temp_name)
            if _sha(target) != expected:
                raise MflacError("output-exists", "output already exists: %s" % target)
        finally:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        if _sha(src) != source_sha:
            raise MflacError("source-changed", "source changed during validation")
        warning = _cache_commit(cache_root, source_sha, material, expected)
        result = {"source":str(rel),"sourceSHA":source_sha,"status":"unchanged",
                  "output":str(out_rel),"outputSHA":expected}
        if warning is not None:
            result["cacheWarning"] = warning
        return result
    candidate = _decrypt_candidate(payload, material)
    fd, temp_name = tempfile.mkstemp(prefix=".qqmflac-", suffix=".part", dir=str(out_root))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(candidate)
        _validate_file(ffmpeg, temp_path, tail_helper, out_root)
        if _sha(src)!=source_sha:raise MflacError('source-changed','source changed during validation')
        if not _inside(target,out_root):raise MflacError('output-escape','output parent changed')
        output_sha = _publish(str(temp_path), str(target))
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    cache_warning = _cache_commit(cache_root, source_sha, material, output_sha)
    return {"source": str(rel), "sourceSHA": source_sha, "status": "ok",
            "output": str(out_rel), "outputSHA": output_sha,"cacheWarning":cache_warning}


def _fail(source, category, message, source_sha=None):
    item = {"source": source, "status": "failed", "category": category, "message": str(message)}
    if source_sha:
        item["sourceSHA"] = source_sha
    return item


def decode_folder(input_dir, output_dir, cache_dir, ffmpeg, tail_helper=None, provider=None, limit=None):
    """Decode one file or a folder; returns a report that contains no material."""
    report = {"ok": False, "algorithm": ALGORITHM, "items": []}
    try:
        if not os.path.isfile(ffmpeg) or not os.access(ffmpeg, os.X_OK):
            raise MflacError("ffmpeg-invalid", "FFmpeg is not an executable file")
        inp = Path(input_dir)
        if not os.path.lexists(inp):
            raise MflacError("input-invalid", "input is unavailable")
        if stat.S_ISLNK(os.lstat(inp).st_mode):
            raise MflacError("input-invalid", "input must not be a symlink")
        is_dir = inp.is_dir()
        root = inp if is_dir else inp.parent
        out_root, cache_root = Path(output_dir), Path(cache_dir)
        for label, path in (("output", out_root), ("cache", cache_root)):
            if os.path.lexists(path) and (stat.S_ISLNK(os.lstat(path).st_mode) or not path.is_dir()):
                raise MflacError("input-invalid", "%s directory is not a real directory" % label)
            if is_dir and _inside(path, inp):
                raise MflacError("input-invalid", "%s must not be inside the input folder" % label)
        out_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        cache_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if _inside(out_root,cache_root) or _inside(cache_root,out_root):raise MflacError('input-invalid','output and private material cache must not overlap')
    except (MflacError, OSError) as exc:
        report.update({"category": getattr(exc, "category", "input-invalid"), "error": str(exc)})
        return report

    found = [inp]
    if is_dir:
        found = []
        for walk_root, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(walk_root, d)))
            found.extend(Path(walk_root) / name for name in sorted(files))
    audio, skipped = [], []
    for path in found:
        name = path.name.lower()
        if re.search(r'\.mgg\d*(?:\.flac)?$',name):
            skipped.append({"source": str(path.relative_to(root)), "status": "skipped",
                            "category": "mgg-unsupported"})
        elif name.endswith(AUDIO_SUFFIXES):
            audio.append(path)
    audio.sort(key=lambda p: str(p))
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit,bool) or limit <= 0:
            report.update({"category": "input-invalid", "error": "limit must be a positive integer"})
            return report
        audio = audio[:limit]

    scanned, missing = [], []
    for src in audio:
        rel = src.relative_to(root)
        try:
            source_sha = _preflight(src)
            cache_entry = _cache_lookup(cache_root, source_sha)
            scanned.append([src, rel, source_sha, cache_entry, None])
            if cache_entry is None and all(s[2] != source_sha for s in scanned[:-1]):
                missing.append(src)
        except MflacError as exc:
            scanned.append([src, rel, None, None, exc])
        except OSError as exc:
            scanned.append([src, rel, None, None, MflacError("input-invalid", str(exc))])

    provided, provider_error = {}, None
    if missing and provider is not None:
        try:
            got = provider.fetch(list(missing))
            if not isinstance(got, dict):
                raise TypeError("provider.fetch did not return a dict")
            provided = got
        except Exception as exc:
            provider_error = "provider failed: %s" % exc

    items, ok_count, fail_count, wait_count = list(skipped), 0, 0, 0
    for src, rel, source_sha, cache_entry, error in scanned:
        if error is not None:
            items.append(_fail(str(rel), error.category, error.message, source_sha))
            fail_count += 1
            continue
        if cache_entry is not None:
            material = cache_entry["material"]
        elif provider_error:
            items.append(_fail(str(rel), "provider-error", provider_error, source_sha))
            fail_count += 1
            continue
        else:
            material = provided.get(source_sha)
        try:
            items.append(_process(src, rel, out_root, cache_root, ffmpeg, tail_helper,
                                  material, source_sha, cache_entry))
            ok_count += 1
        except MflacError as exc:
            items.append(_fail(str(rel), exc.category, exc.message, source_sha))
            if exc.category == "waiting":
                wait_count += 1
            else:
                fail_count += 1
        except OSError as exc:
            items.append(_fail(str(rel), "io-error", str(exc), source_sha))
            fail_count += 1
    report["items"] = items
    report["counts"] = {"ok": ok_count, "failed": fail_count,
                        "waiting": wait_count, "skipped": len(skipped),"unchanged":sum(i['status']=='unchanged' for i in items)}
    report["ok"] = ok_count > 0 and fail_count == 0 and wait_count == 0
    if not audio:
        report.update({"category": "input-invalid", "error": "no MFLAC inputs found"})
    return report
