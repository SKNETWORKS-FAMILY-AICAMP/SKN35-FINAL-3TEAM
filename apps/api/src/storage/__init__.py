"""원본 파일 저장소. DB 에는 파일 자체가 아니라 이 저장소 기준 '상대 경로(키)'만 넣는다.

    from storage import LocalStorage
    store = LocalStorage.from_env()                       # .env 의 STORAGE_ROOT (기본 data/storage)
    key = f"knowledge/{doc_id}/{version}/{file_name}"
    info = store.save(key, data)                          # → StoredFile(key, size, sha256)
    store.read(key); store.delete(key); store.exists(key)

키 규칙 (팀 ERD 주석과 같음)
  knowledge/{doc_id}/{version}/원본   전사 문서        doc_version.storage_path
  work/{session_id}/…                      작업 파일        work_file.storage_path
  outputs/{user_id}/…                      산출물           output_file.storage_path
나중에 고객사 NAS·오브젝트 저장소로 바꿀 땐 같은 메서드를 가진 클래스를 하나 더 만들면 된다 (DB 는 그대로).
"""

from storage.local import LocalStorage, StoredFile, StorageKeyError  # noqa: F401
