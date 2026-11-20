"""원본 파일 저장소 (DB 없이 도는 테스트)."""

import hashlib

import pytest

from storage import LocalStorage, StorageKeyError


def test_save_read_delete_roundtrip(tmp_path):
    store = LocalStorage(tmp_path)
    info = store.save("knowledge/1/1/여신규정.hwpx", b"hello")
    assert info.key == "knowledge/1/1/여신규정.hwpx" and info.size == 5
    assert info.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert store.read(info.key) == b"hello" and store.exists(info.key)
    assert store.delete(info.key) and not store.exists(info.key)
    assert store.delete(info.key) is False          # 두 번째는 없음


def test_original_is_not_overwritten_by_default(tmp_path):
    store = LocalStorage(tmp_path)
    store.save("work/s1/a.pdf", b"1")
    with pytest.raises(FileExistsError):
        store.save("work/s1/a.pdf", b"2")
    store.save("work/s1/a.pdf", b"2", overwrite=True)
    assert store.read("work/s1/a.pdf") == b"2"


@pytest.mark.parametrize("key", ["../x", "a/../../x", "/etc/passwd", "C:/x", "a\\b", "", "a//b", "./a"])
def test_keys_cannot_escape_storage_root(tmp_path, key):
    with pytest.raises(StorageKeyError):
        LocalStorage(tmp_path).save(key, b"x")
