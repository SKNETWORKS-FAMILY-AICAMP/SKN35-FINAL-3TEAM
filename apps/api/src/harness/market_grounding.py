"""시장 전용 에이전트의 조회/저장 순서를 코드에서 보장한다."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MARKET_NAMES = {"market_snapshot", "save_morning_report", "get_morning_report", "today",
                "search_report_templates", "get_report_template"}


def market_plan(question: str, specs):
    names = {s.name for s in specs}
    if not names or not names <= MARKET_NAMES:
        return None  # 메일 등 복합 업무의 기존 하네스 흐름은 유지한다.
    ident = re.search(r"\b[0-9a-f]{32}\b", question)
    if ident and "get_morning_report" in names:
        return "get_morning_report", {"report_id": ident.group()}, False
    if ("search_report_templates" in names and re.search(r"양식|서식|템플릿", question)
            and re.search(r"찾|검색|목록|종류|보여|알려", question) and not re.search(r"만들|작성|생성|저장", question)):
        return "search_report_templates", {"query": "" if re.search(r"목록|종류|어떤.*있", question) else question[:2000]}, False
    if ("market_snapshot" not in names or not (re.search(r"금리|환율|시장.*(?:조회|리포트|보고서)", question)
            or ("search_report_templates" in names and re.search(r"양식|서식|템플릿|브리핑", question)))):
        return None
    if re.search(r"이란|리포트란|무슨\s*뜻|개념|의미", question) and not re.search(r"조회|만들|작성|생성|저장", question):
        return None
    args = {}
    when = re.search(r"(\d{4})[-년]\s*(\d{1,2})[-월]\s*(\d{1,2})", question)
    if when:
        args["base_date"] = f"{when[1]}-{int(when[2]):02d}-{int(when[3]):02d}"
    elif "어제" in question:
        args["base_date"] = (datetime.now(ZoneInfo("Asia/Seoul")).date() - timedelta(days=1)).isoformat()
    elif re.search(r"지난|저번|월요일|화요일|수요일|목요일|금요일|토요일|일요일", question):
        return "clarify", {}, False
    wants_report = bool(re.search(r"리포트|보고서|브리핑|양식|서식|저장", question) and re.search(r"만들|작성|생성|저장|줘|주세요", question))
    return "market_snapshot", args, wants_report
