"""Content-based, no-Frida decoder. Originals are read-only; outputs never replace files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import legacy_decoders as legacy
import qq_mflac as mflac
from audio_validation import clean_flac_tail, validate_ogg_stream
from qq_local_cache import LocalCacheMaterialProvider

MAX_BYTES = 256 * 1024 * 1024
AUDIO_EXT = {
    ".flac", ".mp3", ".ogg", ".opus", ".wav", ".m4a", ".mp4", ".aac",
    ".ncm", ".mflac", ".mgg", ".ape", ".aiff", ".aif", ".wma",
    ".xm", ".x2m", ".x3m", ".kwm", ".kgm", ".kgma", ".kgg", ".vpr",
    ".tm0", ".tm2", ".tm3", ".tm6",
}
WAIT_MESSAGE = (
    "本机 QQ 缓存缺少此文件自己的可用材料。请从桌面打开 QQ 音乐、登录并手动播放"
    "对应的下载曲目一次后重试；脚本不会自动附加进程、读取其他歌曲的密钥或退回 Frida。"
)
KGM_V5_MESSAGE = (
    "KGM/KGG crypto v5 需要独立的酷狗数据库密钥路线；本解码器不猜测、不伪造该路线。"
)


def sniff(head):
    return legacy.sniff_audio(head)


def strict_audio(ffmpeg, path):
    """Full decode plus format-specific integrity proof.

    FLAC uses the STREAMINFO full-PCM MD5.  Ogg additionally must pass complete
    page framing, CRC, per-stream sequence and EOS checks because FFmpeg alone
    can accept a truncated Ogg stream.
    """
    with Path(path).open("rb") as handle:
        header = handle.read(64)
    if header.startswith(b"fLaC"):
        mflac._strict_audio(ffmpeg, path)
    else:
        cmd = [
            str(ffmpeg), "-nostdin", "-v", "error", "-xerror", "-err_detect",
            "explode", "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-f", "null", "-",
        ]
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=600
        )
        if result.returncode or result.stderr:
            detail = result.stderr.decode("utf-8", "replace").strip()[:200]
            raise mflac.MflacError(
                "audio-invalid", "Strict full audio decode failed: %s" % (detail or result.returncode)
            )
        if header.startswith(b"OggS"):
            try:
                validate_ogg_stream(path)
            except ValueError as exc:
                raise mflac.MflacError("audio-invalid", str(exc)) from None
    probe = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    result = subprocess.run(
        [
            str(probe), "-v", "error", "-select_streams", "a:0", "-show_entries",
            "stream=codec_name,sample_rate,channels,bits_per_raw_sample,duration:format=duration",
            "-of", "json", str(path),
        ],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
    )
    if result.returncode or result.stderr:
        raise mflac.MflacError("audio-invalid", "Audio metadata probe failed")
    try:
        data = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise mflac.MflacError("audio-invalid", "Audio metadata probe is not JSON") from None
    streams = data.get("streams", [])
    if len(streams) != 1:
        raise mflac.MflacError("audio-invalid", "No primary audio stream")
    stream = streams[0]
    duration = float(stream.get("duration") or data.get("format", {}).get("duration", 0))
    if not 0 < duration < 86400:
        raise mflac.MflacError("audio-invalid", "Invalid audio duration")
    return {
        "codec": stream.get("codec_name"),
        "sampleRate": int(stream["sample_rate"]),
        "channels": int(stream["channels"]),
        "duration": duration,
        "bits": stream.get("bits_per_raw_sample"),
    }


def decrypt_ncm(data):
    # Source-compatible port of repository ncm-decrypt.mjs; no Node runtime needed.
    from Cryptodome.Cipher import AES
    from Cryptodome.Util.Padding import unpad

    if data[:8] != b"CTENFDAM" or len(data) < 14:
        raise ValueError("Invalid NCM container")
    pos = 10

    def take(n):
        nonlocal pos
        if n < 0 or pos + n > len(data):
            raise ValueError("Truncated NCM container")
        block = data[pos:pos + n]
        pos += n
        return block

    def sized(cap):
        n = struct.unpack("<I", take(4))[0]
        if n > cap:
            raise ValueError("NCM field too large")
        return take(n)

    raw = sized(4096)
    if not raw or len(raw) % 16:
        raise ValueError("Invalid NCM key block")
    key = unpad(AES.new(b"hzHRAmso5kInbaxW", AES.MODE_ECB).decrypt(bytes(x ^ 0x64 for x in raw)), 16)
    if not key.startswith(b"neteasecloudmusic") or len(key) <= 17:
        raise ValueError("Invalid NCM key prefix")
    key = key[17:]
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + key[i % len(key)]) & 255
        box[i], box[j] = box[j], box[i]
    stream = bytes(
        box[(box[i] + box[(box[i] + i) & 255]) & 255]
        for i in range(1, 257) for i in (i & 255,)
    )
    sized(8 * 1024 * 1024)  # External metadata is not rewritten into the audio payload.
    take(9)
    sized(32 * 1024 * 1024)
    if pos >= len(data):
        raise ValueError("NCM has no audio")
    return bytes(value ^ stream[i & 255] for i, value in enumerate(data[pos:]))


def _publish_copy(src, target, ffmpeg, expected_sha, original=None, original_sha=None, root=None):
    original = Path(original or src)
    original_sha = original_sha or expected_sha
    target = Path(target)
    root = Path(root) if root is not None else target.parent

    def check_original():
        if mflac._sha(original) != original_sha:
            raise mflac.MflacError("source-changed", "Source changed before publication")

    target.parent.mkdir(parents=True, exist_ok=True)
    if not mflac._inside(target, root):
        raise ValueError("Output path escaped the output root")
    if os.path.lexists(target):
        mflac._regular(target)
        if mflac._sha(target) != expected_sha:
            raise ValueError("Output exists with different contents")
        info = strict_audio(ffmpeg, target)
        check_original()
        return "unchanged", info
    fd, tmp = tempfile.mkstemp(prefix=".audio-", suffix=".part", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as source:
            shutil.copyfileobj(source, out)
        if mflac._sha(tmp) != expected_sha:
            raise ValueError("Source changed during copy")
        info = strict_audio(ffmpeg, Path(tmp))
        check_original()
        if not mflac._inside(target, root):
            raise ValueError("Output path escaped the output root")
        mflac._publish(tmp, str(target))
        return "ok", info
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _publish_bytes(data, target, ffmpeg, source, source_sha, root):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".decoded-", suffix=".part", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        expected = mflac._sha(tmp)
        return _publish_copy(
            tmp, target, ffmpeg, expected, original=source,
            original_sha=source_sha, root=root,
        )
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_bounded(path):
    before = mflac._regular(path)
    if before.st_size > MAX_BYTES:
        raise mflac.MflacError("too-large", "File exceeds 256 MiB limit")
    with open(path, "rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise mflac.MflacError("too-large", "File exceeds 256 MiB limit")
    return data


def _provider_material(provider, source, source_sha):
    try:
        result = provider.fetch([source])
    except Exception as exc:
        raise mflac.MflacError("provider-error", "local material provider failed") from exc
    if not isinstance(result, dict):
        raise mflac.MflacError("provider-error", "local material provider returned an invalid result")
    return result.get(source_sha)


_MFLAC_NAME_RE = re.compile(r"\.mflac(?:[0-9ahml])?(?:\.flac)?$", re.I)
_MFLAC_IDENTIFIER_RE = re.compile(r"\.mflac(?:[0-9ahml])?(?:\.flac)?$", re.I)


def _fast_mflac_identifier(tail):
    """Return a musicex MediaFileName only when it names a .mflac output."""
    if len(tail) < 192:
        return None
    try:
        identifier = legacy._tag_identifier(
            tail[legacy.MUSICEX_NAME_OFFSET:
                 legacy.MUSICEX_NAME_OFFSET + legacy.MUSICEX_NAME_BYTES]
        )
    except legacy.LegacyError:
        return None
    return identifier if _MFLAC_IDENTIFIER_RE.search(identifier) else None


def _has_repeated_musicex_tail(src):
    """True when a second valid cleartext tag precedes the final 192-byte tag."""
    try:
        with open(src, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size < 2 * 192:
                return False
            handle.seek(size - 2 * 192)
            block = handle.read(192)
    except OSError:
        return False
    return len(block) == 192 and legacy._valid_musicex_tail(block, 192) is not None


def _try_existing_mflac(src, rel, out, cache, ffmpeg, provider, source_sha, tail):
    """Preserve the proven 512-byte MFLAC core unchanged (MFLAC names only)."""
    if _MFLAC_NAME_RE.search(Path(src).name) is None:
        return None
    if tail[-8:] != b"musicex\0":
        return None
    try:
        tag_size = int.from_bytes(tail[-16:-12], "little")
    except (TypeError, ValueError):
        return None
    if tag_size != 192:
        return None
    if _fast_mflac_identifier(tail) is None:
        # The cleartext tag must identify a .mflac layout; MGG always takes the
        # explicit generic QMC branch, even if someone renamed the container.
        return None
    if _has_repeated_musicex_tail(src):
        # Repeated/variable tail layouts are handled by the generic path, which
        # strips the bounded same-identifier tags before decryption.
        return None
    try:
        digest = mflac._preflight(src)
    except mflac.MflacError as exc:
        if exc.category in ("source-changed", "too-large"):
            raise
        return None
    if digest != source_sha:
        raise mflac.MflacError("source-changed", "Source changed during preflight")
    entry = mflac._cache_lookup(cache, digest)
    material = entry["material"] if entry is not None else None
    if material is None:
        material = _provider_material(provider, src, digest)
    if material is None:
        raise mflac.MflacError("waiting", legacy_material_wait_message())
    if len(material) != 512:
        # The proven core is intentionally 512-only.  A non-512 record uses
        # the explicit generic QMC branch instead of being forced through it.
        return ("generic", bytes(material))
    row = mflac._process(
        src, rel, out, cache, str(ffmpeg), clean_flac_tail,
        material, digest, entry,
    )
    row["kind"] = "mflac"
    row["algorithm"] = mflac.ALGORITHM
    row["materialSource"] = "local-cache"
    return ("done", row)


def legacy_material_wait_message():
    return WAIT_MESSAGE


def decode(input_path, output_dir, cache_dir, ffmpeg, qq_cache_root=None, provider=None):
    inp = Path(input_path).absolute()
    out = Path(output_dir).absolute()
    cache = Path(cache_dir).absolute()
    report = dict(ok=False, engine="local-cache-no-frida-v2", items=[])
    if not inp.exists() or inp.is_symlink():
        raise ValueError("Input must exist and must not be a symlink")
    is_dir = inp.is_dir()
    root = inp if is_dir else inp.parent
    for label, path in (("Output", out), ("Cache", cache)):
        if os.path.lexists(path) and (path.is_symlink() or not path.is_dir()):
            raise ValueError("%s must be a real directory" % label)
        if (is_dir and mflac._inside(path, inp)) or (not is_dir and mflac._inside(inp, path)):
            raise ValueError("%s must not overlap input" % label)
    if mflac._inside(out, cache) or mflac._inside(cache, out):
        raise ValueError("Output/cache overlap")
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    paths = []
    if is_dir:
        for base, dirs, files in os.walk(inp, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not (Path(base) / d).is_symlink())
            paths.extend(Path(base) / name for name in sorted(files))
    else:
        paths = [inp]

    if provider is None:
        provider = LocalCacheMaterialProvider(cache_root=qq_cache_root)

    for src in paths:
        rel = src.relative_to(root)
        row = dict(source=str(rel))
        temp = None
        try:
            info = mflac._regular(src)
            if info.st_size > MAX_BYTES:
                raise ValueError("File exceeds 256 MiB limit")
            with src.open("rb") as handle:
                head = handle.read(64)
                tail_length = min(192, info.st_size)
                if tail_length:
                    handle.seek(info.st_size - tail_length)
                    tail = handle.read(tail_length)
                else:
                    tail = b""

            plain = sniff(head)
            kind = None
            if plain:
                kind = "plain"
            elif head[:8] == b"CTENFDAM":
                kind = "ncm"
            else:
                kind = legacy.preview_kind(src.name, head, tail)

            source_sha = mflac._sha(src)
            row.update(sourceSHA=source_sha)

            if kind == "plain":
                target = out / rel.with_suffix(plain)
                if not mflac._inside(target, out):
                    raise ValueError("Output escaped root")
                status, audio = _publish_copy(
                    src, target, ffmpeg, source_sha, original=src,
                    original_sha=source_sha, root=out,
                )
                row.update(
                    status=status, kind="plain", output=str(target.relative_to(out)),
                    outputSHA=source_sha, audio=audio, bytePreserved=True,
                )
            elif kind == "ncm":
                data = _read_bounded(src)
                if hashlib.sha256(data).hexdigest() != source_sha:
                    raise mflac.MflacError("source-changed", "Source changed before decode")
                decoded = decrypt_ncm(data)
                extension = sniff(decoded[:64])
                if extension not in (".flac", ".mp3", ".ogg", ".wav", ".m4a", ".aac", ".mp4"):
                    raise ValueError("Unsupported NCM audio payload")
                fd, name = tempfile.mkstemp(prefix=".ncm-", suffix=extension, dir=out)
                temp = Path(name)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(decoded)
                target = out / rel.with_suffix(extension)
                if not mflac._inside(target, out):
                    raise ValueError("Output escaped root")
                expected = mflac._sha(temp)
                status, audio = _publish_copy(
                    temp, target, ffmpeg, expected, original=src,
                    original_sha=source_sha, root=out,
                )
                row.update(
                    status=status, kind="ncm", output=str(target.relative_to(out)),
                    outputSHA=expected, audio=audio,
                )
            elif kind == "qmc":
                handled = _try_existing_mflac(
                    src, rel, out, cache, ffmpeg, provider, source_sha, tail
                )
                if handled is not None and handled[0] == "done":
                    row.update(handled[1])
                else:
                    data = _read_bounded(src)
                    if hashlib.sha256(data).hexdigest() != source_sha:
                        raise mflac.MflacError("source-changed", "Source changed before decode")
                    footer = legacy.parse_qmc_footer(data)
                    material = None
                    material_source = "none"
                    if footer.kind in ("qtag", "rawkey"):
                        material = footer.material
                        material_source = "embedded"
                    elif footer.kind == "musicex":
                        material = None
                        if handled is not None and handled[0] == "generic":
                            material = handled[1]
                        if material is None:
                            material = _provider_material(provider, src, source_sha)
                        if material is None:
                            raise mflac.MflacError("waiting", legacy_material_wait_message())
                        material_source = "local-cache"
                    decoded = legacy.decrypt_qmc(data, footer, material)
                    target = out / rel.parent / legacy.output_name(rel.name, decoded.extension)
                    if not mflac._inside(target, out):
                        raise ValueError("Output escaped root")
                    status, audio = _publish_bytes(
                        decoded.data, target, ffmpeg, src, source_sha, out
                    )
                    row.update(
                        status=status, kind="qmc", algorithm=decoded.algorithm,
                        footer=decoded.note, output=str(target.relative_to(out)),
                        outputSHA=mflac._sha(target), audio=audio,
                        materialSource=material_source,
                    )
            elif kind in ("kwm", "xiami", "ximalaya", "tm", "kgm"):
                data = _read_bounded(src)
                if hashlib.sha256(data).hexdigest() != source_sha:
                    raise mflac.MflacError("source-changed", "Source changed before decode")
                decoded = legacy.decode_legacy(data, kind)
                target = out / rel.parent / legacy.output_name(rel.name, decoded.extension)
                if not mflac._inside(target, out):
                    raise ValueError("Output escaped root")
                status, audio = _publish_bytes(
                    decoded.data, target, ffmpeg, src, source_sha, out
                )
                row.update(
                    status=status, kind=decoded.kind, algorithm=decoded.algorithm,
                    output=str(target.relative_to(out)), outputSHA=mflac._sha(target),
                    audio=audio, materialSource="none",
                )
            else:
                audio_like = src.suffix.lower() in AUDIO_EXT or qmc_name_like(src.name)
                row.update(
                    status="failed" if audio_like else "skipped",
                    category="unsupported-format",
                )
            # Each publisher checks the source immediately before committing.
            # Changes after that commit do not invalidate the verified snapshot.
        except mflac.MflacError as exc:
            if exc.category == "waiting":
                row.update(status="waiting", category="local-material-missing",
                           message=exc.message)
            else:
                row.update(status="failed", category=exc.category, message=exc.message[:240])
        except legacy.LegacyError as exc:
            if exc.category == "kgm-v5-db-required":
                row.update(status="waiting", category=exc.category, message=KGM_V5_MESSAGE)
            else:
                row.update(status="failed", category=exc.category, message=exc.message[:240])
        except (ValueError, OSError, KeyError, IndexError,
                subprocess.SubprocessError) as exc:
            row.update(status="failed", category=getattr(exc, "category", "validation-failed"),
                       message=str(exc)[:240])
        finally:
            if temp and temp.exists():
                temp.unlink()
        report["items"].append(row)

    counts = {
        state: sum(item["status"] == state for item in report["items"])
        for state in ("ok", "unchanged", "failed", "waiting", "skipped")
    }
    report["counts"] = counts
    report["ok"] = (
        counts["ok"] + counts["unchanged"] > 0
        and counts["failed"] == counts["waiting"] == 0
    )
    return report


def qmc_name_like(name):
    return legacy.qmc_hint(name) is not None
