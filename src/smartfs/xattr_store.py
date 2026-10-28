"""Linux ``user.smartfs.*`` extended-attribute mirror for file metadata."""

from __future__ import annotations

import errno
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from smartfs.models import XattrMetadata
from smartfs.suppression import GLOBAL_SUPPRESSOR, normalize_path

XATTR_PREFIX = "user.smartfs."
CATEGORY_ATTR = XATTR_PREFIX + "category"
TAGS_ATTR = XATTR_PREFIX + "tags"
SUMMARY_ATTR = XATTR_PREFIX + "summary"
HASH_ATTR = XATTR_PREFIX + "hash"
VERSION_ATTR = XATTR_PREFIX + "version"
PROBE_ATTR = XATTR_PREFIX + "probe"
SMARTFS_ATTRS = (CATEGORY_ATTR, TAGS_ATTR, SUMMARY_ATTR, HASH_ATTR, VERSION_ATTR)
SUMMARY_MAX_BYTES = 512
PROBE_PREFIX = ".smartfs-xattr-probe-"

_UNSUPPORTED_ERRNOS = {
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
    errno.EPERM,
    errno.EACCES,
    errno.ENOSPC,
    errno.E2BIG,
    errno.EROFS,
}

_REASON_BY_ERRNO = {
    errno.ENOTSUP: "filesystem does not support user extended attributes",
    errno.EOPNOTSUPP: "filesystem does not support user extended attributes",
    errno.EPERM: "permission denied by the filesystem",
    errno.EACCES: "permission denied by the filesystem",
    errno.ENOSPC: "no space left for extended attributes",
    errno.E2BIG: "extended attribute value is too large",
    errno.EROFS: "filesystem is read-only",
}


class XattrError(RuntimeError):
    """Base error for extended-attribute operations."""


class XattrUnsupported(XattrError):
    """Raised when the path's filesystem cannot store user xattrs."""


class XattrOperationError(XattrError):
    """Raised when an xattr operation fails for a non-capability reason."""


@dataclass(frozen=True, slots=True)
class XattrSupport:
    """Result of probing a path for user xattr support."""

    supported: bool
    reason: str | None = None
    error_number: int | None = None


@dataclass(slots=True)
class XattrWriteResult:
    """Outcome of mirroring SmartFS metadata to one file."""

    written: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.written or self.removed)


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Truncate a string to at most ``max_bytes`` UTF-8 bytes on a char boundary."""
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def one_line(value: str) -> str:
    """Collapse all whitespace into single spaces and remove NUL bytes."""
    return " ".join(value.replace("\x00", " ").split())


def _reason_for_errno(error_number: int) -> str:
    return _REASON_BY_ERRNO.get(error_number, os.strerror(error_number))


def probe_xattr_support(path: str | os.PathLike[str]) -> XattrSupport:
    """Probe xattr read/write/remove support without modifying ``path`` itself."""
    target = Path(path).expanduser()
    probe_dir = target if target.is_dir() else target.parent
    if not probe_dir.exists():
        return XattrSupport(False, "probe directory does not exist", errno.ENOENT)
    probe_path: str | None = None
    try:
        fd, probe_path = tempfile.mkstemp(prefix=PROBE_PREFIX, dir=probe_dir)
        os.close(fd)
        os.setxattr(probe_path, PROBE_ATTR, b"smartfs", flags=0)
        value = os.getxattr(probe_path, PROBE_ATTR)
        if value != b"smartfs":
            return XattrSupport(False, "xattr probe returned unexpected data", None)
        os.removexattr(probe_path, PROBE_ATTR)
        return XattrSupport(True)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_ERRNOS:
            return XattrSupport(False, _reason_for_errno(exc.errno), exc.errno)
        return XattrSupport(False, f"xattr probe failed: {os.strerror(exc.errno or 0)}", exc.errno)
    finally:
        if probe_path is not None:
            try:
                os.unlink(probe_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def supports_xattr(path: str | os.PathLike[str]) -> bool:
    """Return True when the path's filesystem supports ``user.*`` xattrs."""
    return probe_xattr_support(path).supported


def _raise_for_oserror(exc: OSError, path: str | os.PathLike[str]) -> None:
    message = f"{_reason_for_errno(exc.errno or 0)}: {path}"
    if exc.errno in _UNSUPPORTED_ERRNOS:
        raise XattrUnsupported(message) from exc
    raise XattrOperationError(message) from exc


def read_metadata(path: str | os.PathLike[str]) -> XattrMetadata:
    """Read SmartFS metadata from a file's xattrs.

    Missing attributes are treated as empty values. Capability and permission
    errors raise :class:`XattrUnsupported`.
    """
    raw: dict[str, str] = {}
    for name in SMARTFS_ATTRS:
        try:
            value = os.getxattr(path, name)
        except OSError as exc:
            if exc.errno == errno.ENODATA:
                continue
            if exc.errno == errno.ENOTSUP or exc.errno == errno.EOPNOTSUPP:
                _raise_for_oserror(exc, path)
            if exc.errno in {errno.EPERM, errno.EACCES}:
                _raise_for_oserror(exc, path)
            raise XattrOperationError(
                f"Failed to read {name}: {os.strerror(exc.errno or 0)}"
            ) from exc
        else:
            raw[name] = value.decode("utf-8", errors="replace")
    tags = [tag.strip() for tag in raw.get(TAGS_ATTR, "").split(",") if tag.strip()]
    return XattrMetadata(
        category=raw.get(CATEGORY_ATTR) or None,
        tags=tags,
        summary=raw.get(SUMMARY_ATTR) or None,
        sha256=raw.get(HASH_ATTR) or None,
        version=raw.get(VERSION_ATTR) or None,
    )


def list_smartfs_xattrs(path: str | os.PathLike[str]) -> dict[str, str]:
    """Return all SmartFS-prefixed xattrs as decoded strings."""
    result: dict[str, str] = {}
    try:
        names = os.listxattr(path)
    except OSError as exc:
        _raise_for_oserror(exc, path)
    for name in names:
        if not name.startswith(XATTR_PREFIX):
            continue
        try:
            value = os.getxattr(path, name)
        except OSError as exc:
            if exc.errno == errno.ENODATA:
                continue
            _raise_for_oserror(exc, path)
        result[name] = value.decode("utf-8", errors="replace")
    return result


def _set_or_remove(
    path: str | os.PathLike[str],
    name: str,
    desired: bytes | None,
    result: XattrWriteResult,
) -> None:
    try:
        current = os.getxattr(path, name)
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            current = None
        else:
            _raise_for_oserror(exc, path)
    if desired is None:
        if current is None:
            result.unchanged.append(name)
            return
        try:
            os.removexattr(path, name)
        except OSError as exc:
            if exc.errno == errno.ENODATA:
                result.unchanged.append(name)
                return
            _raise_for_oserror(exc, path)
        result.removed.append(name)
        return
    if current == desired:
        result.unchanged.append(name)
        return
    try:
        os.setxattr(path, name, desired, flags=0)
    except OSError as exc:
        _raise_for_oserror(exc, path)
    result.written.append(name)


def write_metadata(
    path: str | os.PathLike[str],
    metadata: XattrMetadata,
    *,
    suppression_ttl: float | None = None,
    suppress_events: bool = True,
) -> XattrWriteResult:
    """Mirror validated metadata to xattrs, marking watcher self-writes."""
    if not os.path.isfile(path):
        raise XattrOperationError(f"Not a regular file: {path}")
    summary = one_line(metadata.summary or "")
    category = one_line(metadata.category or "").lower()
    tags = sorted({tag.strip() for tag in metadata.tags if tag.strip()})
    desired: dict[str, bytes | None] = {
        CATEGORY_ATTR: category.encode("utf-8") if category else None,
        TAGS_ATTR: ",".join(tags).encode("utf-8") if tags else None,
        SUMMARY_ATTR: (
            truncate_utf8(summary, SUMMARY_MAX_BYTES).encode("utf-8") if summary else None
        ),
        HASH_ATTR: (metadata.sha256 or "").encode("utf-8") or None,
        VERSION_ATTR: (metadata.version or "").encode("utf-8") or None,
    }
    if suppress_events:
        GLOBAL_SUPPRESSOR.mark(path, suppression_ttl)
    result = XattrWriteResult()
    for name in SMARTFS_ATTRS:
        _set_or_remove(path, name, desired[name], result)
    return result


def remove_metadata(
    path: str | os.PathLike[str],
    *,
    suppress_events: bool = True,
) -> XattrWriteResult:
    """Remove all SmartFS xattrs from a file."""
    if suppress_events:
        GLOBAL_SUPPRESSOR.mark(path)
    result = XattrWriteResult()
    for name in SMARTFS_ATTRS:
        _set_or_remove(path, name, None, result)
    return result


def xattr_path_key(path: str | os.PathLike[str]) -> str:
    """Expose the suppression path key for callers that need it."""
    return normalize_path(path)
