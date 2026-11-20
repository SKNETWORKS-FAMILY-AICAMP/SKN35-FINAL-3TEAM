import json
import asyncio
from datetime import datetime , date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from contracts.permission import ToolCall, Principal
from contracts.harness import ToolSpec, ToolResult

WEEKDAYS = (
    "월요일", "화요일", "수요일", "목요일",
    "금요일", "토요일", "일요일",
)

TODAY_SPEC = ToolSpec(
    name="today",
    display_name="날짜 확인",
    effect="read",
    domain="날짜",
    description=(
        "한국 시간 기준 오늘 날짜와 요일을 확인하고, 기준일에서 N일 뒤 날짜를 계산합니다. "
        "달력일 계산과 영업일 계산(주말과 휴일 제외)을 모두 할 수 있습니다. "
        "weekday는 오늘, base_weekday는 기준일, target_weekday는 계산 결과의 요일입니다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "base_date": {
                "type": "string",
                "description": "계산 기준일. YYYY-MM-DD 형식이며 생략하면 한국 기준 오늘",
            },
            "days": {
                "type": "integer",
                "description": "더할 일수. calendar는 음수도 지원하고 business는 0 이상만 지원",
            },
            "mode" : {
                "type": "string",
                "enum": ["calendar", "business"],
                "description": "calendar는 달력일, business는 주말과 서버의 테스트용 휴일 파일에 있는 날짜를 제외한 일수",
            },
        },
        "additionalProperties": False,
    },
)

def get_today() -> dict:
    timezone = ZoneInfo("Asia/Seoul")
    now = datetime.now(timezone)
    return {
        "date" : now.date().isoformat(),
        "weekday": WEEKDAYS[now.weekday()],
        "timezone" : "Asia/Seoul",
    }

def calculate_target_date(base_date: str, days: int) -> str:
    start = date.fromisoformat(base_date)
    period = timedelta(days= days)
    target = start + period
    return target.isoformat()

def load_holidays(path: Path) -> set[date]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError("휴일 데이터는 객체여야 합니다.")

    values = payload.get("dates")
    if not isinstance(values, list):
        raise ValueError("dates는 리스트여야 합니다.")

    holidays = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("휴일은 날짜 문자열이어야 합니다.")
        holidays.add(date.fromisoformat(value))

    return holidays


def calculate_business_date(base_date: str, days: int, holidays: set[date],) -> str:
    if type(days) is not int or days < 0:
        raise ValueError("영업일 수는 0 이상의 정수여야 합니다.")
    target = date.fromisoformat(base_date)
    remaining = days

    while remaining > 0:
        target += timedelta(days=1)

        is_weekday = target.weekday() < 5
        is_holiday = target in holidays

        if is_weekday and not is_holiday:
            remaining -= 1

    return target.isoformat()

class TodayTool:
    spec = TODAY_SPEC

    def __init__(self, holiday_path: Path) -> None:
        self.holiday_path = holiday_path

    async def run(self, call: ToolCall) -> ToolResult:
        payload = get_today()
        mode = call.arguments.get("mode", "calendar")

        if mode not in ("calendar", "business"):
            return ToolResult(call.call_id, False, "계산 방식은 calendar 또는 business입니다.",)

        if "mode" in call.arguments and "days" not in call.arguments:
            return ToolResult(call.call_id, False, "계산 방식을 지정하려면 days도 입력해 주세요.",)

        if "base_date" in call.arguments and "days" not in call.arguments:
            return ToolResult(call.call_id, False, "기준일로 계산하려면 days도 입력해 주세요.")

        if "days" in call.arguments:
            days = call.arguments["days"]
            if type(days) is not int:
                return ToolResult(call.call_id, False, "일수는 정수로 입력해 주세요.")

            base_date = call.arguments.get("base_date", payload["date"])
            if not isinstance(base_date, str):
                return ToolResult(call.call_id, False, "기준일은 날짜 문자열로 입력해 주세요.")

            holidays = set()
            if mode == "business":
                try:
                    holidays = await asyncio.to_thread(
                        load_holidays,
                        self.holiday_path,
                    )
                except (OSError, UnicodeError, ValueError):
                    return ToolResult(
                        call.call_id,
                        False,
                        "휴일 데이터를 읽을 수 없어 영업일을 계산하지 못했습니다.",
                    )

            try:
                if mode == "business":
                    target_date = calculate_business_date(base_date, days, holidays)
                else:
                    target_date = calculate_target_date(base_date, days)
            except (ValueError, OverflowError):
                return ToolResult(
                    call.call_id,
                    False,
                    "유효한 기준일과 계산 가능한 일수를 입력해 주세요. "
                    "영업일 계산은 0 이상의 일수를 지원합니다.",
                )

            payload.update({
                "base_date": base_date,
                "base_weekday": WEEKDAYS[date.fromisoformat(base_date).weekday()],
                "days": days,
                "mode": mode,
                "target_date": target_date,
                "target_weekday": WEEKDAYS[date.fromisoformat(target_date).weekday()],
                "calculation_basis" : (
                    "토요일·일요일과 테스트용 휴일 파일의 날짜 제외"
                    if mode == "business" else "모든 날짜 포함"
                ),
            })

        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload,)
