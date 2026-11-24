"""내 담당 대출 조회 (UC-03·UC-05 의 INC-3).

담당자는 로그인 신원(principal.user_id)으로만 정한다. 툴 입력에 담당자를 고르는 칸이 없으므로
모델이 다른 담당자의 고객을 조회할 방법이 없다 (UC-03 대안 흐름 2a).
분류(만기 경과 / 7일 이내 / 8~30일)와 "먼저 볼 것"은 코드가 정한다. 모델은 안내 문장만 쓴다.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import Principal, ToolCall

OVERDUE, WITHIN_7, WITHIN_30 = "만기 경과", "7일 이내", "8~30일"
BUCKETS = (OVERDUE, WITHIN_7, WITHIN_30)
# 시연용 사후관리 문구. 실제 내규(여신규정·신용관리규정) 용어는 내규 데이터가 들어오면 맞춘다.
FOLLOW_UP = {
    OVERDUE: "상환 또는 기한연장 처리 여부 확인",
    WITHIN_7: "고객의 상환·기한연장 의사 확인",
    WITHIN_30: "만기 사전 안내",
}
FIRST_LOOK_MAX = 3
NO_LOANS = "담당 고객이 없습니다."


class LoanRecord(BaseModel):
    model_config = ConfigDict(strict=True)

    loan_id: str = Field(min_length=1)
    officer_id: str = Field(min_length=1)
    customer_name: str = Field(min_length=1)
    customer_type: str = Field(pattern="^(individual|corporate)$")
    email: str | None
    product: str
    balance: int = Field(ge=0)
    maturity_date: str
    early_warning: bool = False


def mask_name(name: str, customer_type: str) -> str:
    """개인 고객 이름은 가운데 글자를 가린다 (홍길동 → 홍*동, 김철 → 김*). 법인 이름은 그대로."""
    if customer_type != "individual" or len(name) < 2:
        return name
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]


def won(amount: int) -> str:
    """150000000 → '1억 5,000만원' (안내 문장의 숫자 검사도 이 표기를 기준으로 한다)."""
    eok, man = divmod(amount // 10_000, 10_000)
    rest = amount % 10_000
    parts = ([f"{eok:,}억"] if eok else []) + ([f"{man:,}만"] if man else [])
    text = " ".join(parts) + "원" if parts else ""
    return text if not rest else f"{amount:,}원"


def loan_item(loan: LoanRecord, as_of: date) -> dict:
    d_day = (date.fromisoformat(loan.maturity_date) - as_of).days
    bucket = OVERDUE if d_day < 0 else WITHIN_7 if d_day <= 7 else WITHIN_30
    return {
        "loan_id": loan.loan_id,
        "customer": mask_name(loan.customer_name, loan.customer_type),
        "customer_type": "개인" if loan.customer_type == "individual" else "법인",
        "email": loan.email,
        "product": loan.product,
        "balance": won(loan.balance),
        "maturity_date": loan.maturity_date,
        "days_left": d_day,
        "bucket": bucket,
        "early_warning": loan.early_warning,
        "follow_up": FOLLOW_UP[bucket] + (", 조기경보 표시 확인" if loan.early_warning else ""),
    }


class LoanStore:
    """가상 여신 시스템 어댑터. 지금은 JSON 파일을 읽고, DB가 붙으면 이 클래스만 바꾼다."""

    def __init__(self, path: Path, as_of: date | None = None) -> None:
        self.path = path
        self._as_of = as_of  # 없으면 파일의 as_of, 그것도 없으면 오늘

    def _load(self) -> tuple[date, list[LoanRecord]]:
        with self.path.open(encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload, dict) or not isinstance(payload.get("loans"), list):
            raise ValueError("여신 데이터는 loans 목록을 가진 객체여야 합니다.")
        loans = [LoanRecord.model_validate(item) for item in payload["loans"]]
        ids = [loan.loan_id for loan in loans]
        if len(ids) != len(set(ids)):
            raise ValueError("중복된 대출 번호가 있습니다.")
        as_of = self._as_of or (date.fromisoformat(payload["as_of"]) if payload.get("as_of") else date.today())
        return as_of, loans

    def customer_emails(self) -> dict[str, str]:
        """고객 이메일(소문자) → 담당자 ID. 메일 수신자 검사(permission/customer_mail.py)가 쓴다."""
        _, loans = self._load()
        return {loan.email.lower(): loan.officer_id for loan in loans if loan.email}

    def officers(self) -> list[str]:
        """대출을 맡은 담당자 ID (예약 실행 만기 알림이 이 사람들에게 보낸다)."""
        _, loans = self._load()
        return sorted({loan.officer_id for loan in loans})

    def items_for_email(self, principal: Principal, email: str) -> list[dict]:
        """이 담당자의 대출 중 고객 이메일이 email 인 것 (메일 본문 검사용)."""
        as_of, loans = self._load()
        email = email.strip().lower()
        return [loan_item(loan, as_of) for loan in loans
                if loan.officer_id == principal.user_id and (loan.email or "").lower() == email]

    def own_item(self, principal: Principal, loan_id: str) -> dict | None:
        """내 담당 대출 1건. 다른 담당자 것이거나 없으면 None (둘을 구분하지 않는다)."""
        as_of, loans = self._load()
        return next((loan_item(loan, as_of) for loan in loans
                     if loan.loan_id == loan_id and loan.officer_id == principal.user_id), None)

    def maturing(self, principal: Principal, days: int) -> dict:
        as_of, loans = self._load()
        mine = [loan for loan in loans if loan.officer_id == principal.user_id]
        items = [item for item in (loan_item(loan, as_of) for loan in mine) if item["days_left"] <= days]
        items.sort(key=lambda item: (BUCKETS.index(item["bucket"]), item["days_left"]))
        urgent = [item for item in items if item["bucket"] != WITHIN_30 or item["early_warning"]]
        urgent.sort(key=lambda item: (item["bucket"] != OVERDUE, not item["early_warning"], item["days_left"]))
        return {
            "as_of": as_of.isoformat(),
            "days": days,
            "has_customers": bool(mine),
            "message": "" if mine else NO_LOANS,
            "total": len(items),
            "counts": {bucket: sum(item["bucket"] == bucket for item in items) for bucket in BUCKETS},
            "first_look": [item["loan_id"] for item in urgent[:FIRST_LOOK_MAX]],
            "first_look_items": urgent[:FIRST_LOOK_MAX],
            "items": items,
        }


class MaturingInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    days: int = Field(default=30, ge=1, le=90, description="오늘부터 며칠 안에 만기가 오는 대출을 볼지 (1~90, 기본 30). 만기가 지난 대출은 항상 포함")


LOANS_MATURING_SPEC = ToolSpec(
    name="loans_maturing",
    display_name="내 담당 만기 대출 조회",
    effect="read",
    domain="여신",
    description=(
        "로그인한 직원이 담당하는 대출 중 기간 안에 만기가 오거나 이미 지난 대출을 조회합니다. "
        "만기 경과 / 7일 이내 / 8~30일 분류, 사후관리 항목, 먼저 볼 대출(first_look)을 함께 돌려줍니다. "
        "개인 고객 이름은 가운데 글자를 가려 돌려줍니다. 다른 담당자의 고객은 조회할 수 없습니다."
    ),
    parameters=MaturingInput.model_json_schema(),
)


class LoansMaturingTool:
    spec = LOANS_MATURING_SPEC

    def __init__(self, store: LoanStore) -> None:
        self.store = store

    async def run(self, call: ToolCall) -> ToolResult:
        try:
            arguments = MaturingInput.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.call_id, False, "days 는 1~90 사이 정수로 입력해 주세요.")
        try:
            payload = await asyncio.to_thread(self.store.maturing, call.principal, arguments.days)
        except (OSError, ValueError, KeyError):
            return ToolResult(call.call_id, False, "여신 데이터를 읽을 수 없습니다.")
        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)
