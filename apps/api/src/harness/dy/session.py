# 지금까지 무슨 대화를 나눴는가

import json
from pathlib import Path
from dataclasses import asdict

from contracts.harness import Message

class Session:
    def __init__(self, session_id, storage_dir="sessions"):
        self.session_id = session_id
        self.messages: list[Message] = []
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.storage_dir / f"{session_id}.json"
        self.load_messages()

    def add_message(self, message: Message):
        # 메시지를 메모리와 디스크에 저장
        self.messages.append(message)
        self.rewrite_disk()


    def load_messages(self):
        # 파일에서 기록 복원
        if not self.file_path.exists():
            self.messages = []
            return

        with open(self.file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.messages = [
            Message.from_dict(item)
            for item in data
        ]

    def get_history(self):
        # 숨긴건 제외하고 가져오기
        return list(self.messages)

    def rewrite_disk(self):
        # 디스크에 기록을 다시 쓰기
        data = [
            message.to_dict()
            for message in self.messages
        ]
        
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)