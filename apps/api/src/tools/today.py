import json
from datetime import datetime
from zoneinfo import ZoneInfo

from contracts.permission import ToolCall, Principal
from contracts.harness import ToolSpec, ToolResult

TODAY_SPEC = ToolSpec(
    name="today",
    display_name="날짜 확인",
    description="한국 시간 기준 오늘 날짜와 요일을 확인합니다.",
    parameters={
        "type" : "object",
        "properties" : {},
        "additionalProperties" : False,
    },
)

def get_today() -> dict:
    timezone = ZoneInfo("Asia/Seoul")
    now = datetime.now(timezone)
    weekdays = [
        "월요일", "화요일", "수요일", "목요일",
        "금요일", "토요일", "일요일",
    ]
    return {
        "date" : now.date().isoformat(),
        "weekday": weekdays[now.weekday()],
        "timezone" : "Asia/Seoul",
    }

class TodayTool:
    spec = TODAY_SPEC

    async def run(self, call: ToolCall) -> ToolResult:
        payload = get_today()
        return ToolResult(
            call.call_id,
            True,
            json.dumps(payload, ensure_ascii=False),
            data=payload,
        )