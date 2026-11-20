"""로컬 디스크 저장소 — STORAGE_ROOT 아래에 키(상대 경로) 그대로 파일을 둔다."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ROOT = REPO_ROOT / "data" / "storage"


class StorageKeyError(ValueError):
    """키가 저장소 밖을 가리키거나 형식이 잘못됨."""


@dataclass(frozen=True)
class StoredFile:
    key: str      # DB 의 storage_path 에 넣을 값
    size: int     # 바이트 (work_file.size_bytes)
    sha256: str   # 64자 16진수 (document_version.sha256 등 — 중복 등록 막기·감사)


class LocalStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    @classmethod
    def from_env(cls) -> LocalStorage:
        return cls(os.environ.get("STORAGE_ROOT") or DEFAULT_ROOT)

    def _path(self, key: str) -> Path:
        """키 → 실제 경로. '/' 로 나눈 상대 경로만 받고, '..'·절대 경로·드라이브 문자로 저장소 밖을 가리키면 거부."""
        if not key or "\\" in key or ":" in key:
            raise StorageKeyError(f"잘못된 키: {key!r}")
        parts = key.split("/")   # 원문 그대로 나눈다 (경로 객체는 'a//b'·'./a' 를 조용히 정리해 버림)
        if any(p in ("", ".", "..") for p in parts):   # 빈 조각 = 맨 앞 '/'(절대 경로)·'//' 포함
            raise StorageKeyError(f"잘못된 키: {key!r}")
        path = self.root.joinpath(*parts).resolve()
        if self.root not in path.parents:
            raise StorageKeyError(f"저장소 밖을 가리키는 키: {key!r}")
        return path

    def save(self, key: str, data: bytes, overwrite: bool = False) -> StoredFile:
        """저장하고 크기·sha256 을 돌려준다. 같은 키가 있으면 기본은 거부 (원본은 덮어쓰지 않는다)."""
        path = self._path(key)
        if path.exists() and not overwrite:
            raise FileExistsError(f"이미 있는 키: {key}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")   # 다 쓴 뒤에 이름을 바꿔, 중간에 끊겨도 반쪽 파일이 남지 않게
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return StoredFile(key, len(data), hashlib.sha256(data).hexdigest())

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def local_path(self, key: str) -> Path:
        """파서처럼 파일 경로가 필요한 곳에 넘길 실제 경로."""
        return self._path(key)

    def delete(self, key: str) -> bool:
        """지웠으면 True, 원래 없었으면 False."""
        path = self._path(key)
        if not path.exists():
            return False
        path.unlink()
        return True
