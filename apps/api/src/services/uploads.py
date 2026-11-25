"""Uploaded documents and their server-owned identity/ownership metadata."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 5
ALLOWED_EXTENSIONS = {
    ".hwp", ".hwpx", ".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".eml",
    ".csv", ".tsv", ".md", ".markdown", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp",
}


class UploadStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory).resolve()

    def _connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.directory / "uploads.sqlite3", timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("""CREATE TABLE IF NOT EXISTS uploads (
            file_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
            filename TEXT NOT NULL, size_bytes INTEGER NOT NULL
        )""")
        return connection

    def create(self, owner: str, filename: str, data: bytes) -> dict:
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not filename or len(filename) > 200 or any(ord(char) < 32 for char in filename):
            raise ValueError("유효한 파일 이름을 사용해 주세요.")
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError("지원하는 문서·이미지 형식의 파일을 선택해 주세요.")
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("파일은 비어 있지 않은 20MB 이하의 문서여야 합니다.")
        file_id = uuid4().hex + extension
        path = self.directory / file_id
        temporary = self.directory / (file_id + ".part")
        try:
            with closing(self._connect()) as connection, connection:
                temporary.write_bytes(data)
                temporary.replace(path)
                connection.execute("INSERT INTO uploads VALUES (?, ?, ?, ?)",
                                   (file_id, owner, filename, len(data)))
        except BaseException:
            temporary.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise
        return {"file_id": file_id, "filename": filename, "size_bytes": len(data)}

    def get_owned(self, file_id: str, owner: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT file_id, filename, size_bytes FROM uploads WHERE file_id=? AND owner=?",
                (file_id, owner),
            ).fetchone()
        if row is None:
            return None
        path = (self.directory / row["file_id"]).resolve()
        if path.parent != self.directory or not path.is_file():
            return None
        return dict(row)


ATTACHMENT_MARKER = "\n\n[사용자 첨부 파일 목록]\n"


def attachment_context(content: str, attachments: list[dict]) -> str:
    if not attachments:
        return content
    prompt = content.strip() or "첨부한 문서를 읽고 핵심 내용을 정리해 주세요."
    metadata = [{"file_id": item["file_id"], "filename": item["filename"]} for item in attachments]
    return prompt + ATTACHMENT_MARKER + json.dumps(metadata, ensure_ascii=False)


def split_attachment_context(content: str) -> tuple[str, list[dict]]:
    """attachment_context 의 반대: 저장된 질문을 화면용 글과 첨부 목록(file_id, filename)으로 나눈다."""
    text, marker, listing = content.partition(ATTACHMENT_MARKER)
    if not marker:
        return content, []
    try:
        files = json.loads(listing)
    except ValueError:
        return content, []
    if not isinstance(files, list):
        return content, []
    return text, [{"file_id": str(f.get("file_id", "")), "filename": str(f.get("filename", ""))}
                  for f in files if isinstance(f, dict)]
