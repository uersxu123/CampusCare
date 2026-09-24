from __future__ import annotations

import hashlib
from pathlib import Path

from app.services.knowledge_ingestion.models import StoredArtifact


class LocalArtifactStore:
    def __init__(self, root: Path, *, max_bytes: int):
        self.root = root.resolve()
        self.max_bytes = max_bytes

    def put(self, filename: str, data: bytes, mime_type: str) -> StoredArtifact:
        self._validate_filename(filename)
        if not data:
            raise ValueError("上传文件不能为空")
        if len(data) > self.max_bytes:
            raise ValueError(f"上传文件超过大小限制：{self.max_bytes} bytes")
        digest = hashlib.sha256(data).hexdigest()
        storage_key = f"sha256/{digest[:2]}/{digest}"
        path = (self.root / storage_key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Artifact storage key 越界")
        created = not path.exists()
        if created:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return StoredArtifact(
            storage_key=storage_key,
            original_filename=filename,
            mime_type=mime_type,
            byte_size=len(data),
            sha256=digest,
            path=path,
            created=created,
        )

    def read(self, storage_key: str) -> bytes:
        path = (self.root / storage_key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Artifact storage key 越界")
        if not path.is_file():
            raise ValueError("artifact_not_found")
        return path.read_bytes()

    @staticmethod
    def _validate_filename(filename: str) -> None:
        value = (filename or "").strip()
        if not value or value in {".", ".."} or "/" in value or "\\" in value or Path(value).is_absolute():
            raise ValueError("上传文件名包含非法路径")
