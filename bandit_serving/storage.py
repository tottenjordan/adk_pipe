"""Artifact + checkpoint storage for the bandit predictor (contracts §7).

``experiment.json`` is read from ``AIP_STORAGE_URI`` (read-only on Vertex) and
checkpoints go to the writable ``BANDIT_CHECKPOINT_URI``; both are a local
directory (tests, the CPR local endpoint) or a ``gs://bucket/prefix``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

MAX_LOGGED_ERROR = 300  # chars of a storage error kept in a warning


class Storage(Protocol):
    def read_bytes(self, rel: str) -> bytes | None: ...
    def write_bytes(self, rel: str, data: bytes) -> None: ...
    def list_files(self, suffix: str) -> list[tuple[str, float]]:
        """``(name, modified time)`` of the files directly under the root whose
        name ends with ``suffix``."""
        ...

    def delete(self, rel: str) -> None:
        """Delete ``rel``; a missing file is fine."""
        ...


class LocalStorage:
    """A local directory (tests, the CPR local endpoint's mounted model dir)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def read_bytes(self, rel: str) -> bytes | None:
        try:  # no exists() check first: a concurrent prune may delete the file
            return (self.root / rel).read_bytes()
        except FileNotFoundError:
            return None

    def write_bytes(self, rel: str, data: bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def list_files(self, suffix: str) -> list[tuple[str, float]]:
        if not self.root.is_dir():
            return []
        return [
            (f.name, f.stat().st_mtime)
            for f in self.root.iterdir()
            if f.is_file() and f.name.endswith(suffix)
        ]

    def delete(self, rel: str) -> None:
        (self.root / rel).unlink(missing_ok=True)


class GcsStorage:
    """A ``gs://bucket/prefix`` (Vertex sets ``AIP_STORAGE_URI`` to this)."""

    def __init__(self, uri: str) -> None:
        from google.cloud import storage  # lazy: only on Vertex

        bucket, _, prefix = uri.removeprefix("gs://").partition("/")
        self._bucket = storage.Client().bucket(bucket)
        self._prefix = prefix.strip("/")

    def _blob(self, rel: str) -> Any:
        return self._bucket.blob(f"{self._prefix}/{rel}" if self._prefix else rel)

    def read_bytes(self, rel: str) -> bytes | None:
        from google.api_core.exceptions import NotFound

        try:
            return self._blob(rel).download_as_bytes()
        except NotFound:
            return None

    def write_bytes(self, rel: str, data: bytes) -> None:
        self._blob(rel).upload_from_string(data)

    def list_files(self, suffix: str) -> list[tuple[str, float]]:
        prefix = f"{self._prefix}/" if self._prefix else ""
        out = []
        for blob in self._bucket.list_blobs(prefix=prefix):
            name = blob.name[len(prefix) :]
            if name.endswith(suffix) and "/" not in name:
                updated = getattr(blob, "updated", None)
                out.append((name, updated.timestamp() if updated else 0.0))
        return out

    def delete(self, rel: str) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self._blob(rel).delete()
        except NotFound:
            pass


def open_storage(artifacts_uri: str) -> Storage:
    if artifacts_uri.startswith("gs://"):
        return GcsStorage(artifacts_uri)
    return LocalStorage(artifacts_uri.removeprefix("file://"))


def short_error(exc: BaseException) -> str:
    """``Type: message`` on one line, capped (GCS errors carry long bodies)."""
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    if len(text) > MAX_LOGGED_ERROR:
        return text[: MAX_LOGGED_ERROR - 1] + "…"
    return text


def _natural_key(name: str) -> tuple[Any, ...]:
    """``exp-e1-v10`` sorts after ``exp-e1-v9`` (digit runs compare as numbers)."""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name))


def prune_checkpoints(storage: Storage, keep_name: str, keep: int) -> None:
    """Delete all but the newest ``keep`` ``.npz`` files (``keep_name``, the one
    ``latest.json`` points at, always stays). Failures only log."""
    try:
        files = storage.list_files(".npz")
    except Exception as exc:
        log.warning("checkpoint prune: listing failed: %s", short_error(exc))
        return
    files.sort(key=lambda f: (f[1], _natural_key(f[0])), reverse=True)
    kept = {keep_name}
    for name, _ in files:
        if len(kept) >= max(keep, 1):
            break
        kept.add(name)
    for name in (n for n, _ in files if n not in kept):
        try:
            storage.delete(name)
        except Exception as exc:
            log.warning(
                "checkpoint prune: delete %s failed: %s", name, short_error(exc)
            )
