# [참고용] dy 가 만든 문서 권한(ABAC) + 보고서 작성 프로토타입. 하네스·API 와 연결되지 않은 독립 스크립트다.
# 나중에 문서 검색 툴(search_knowledge)을 만들 때 부서·보안등급으로 문서를 거르는 규칙의 참고 자료로 쓴다.
# 실행: uv run python scripts/reference/abac_report_demo.py   (결과는 logs/report_runs/ 에 저장)

# 보고서 에이전트의 ABAC 검증을 위한 가짜 데이터.

import json
from pathlib import Path
from uuid import uuid4

USERS = {
    "employee_a": {
        "user_id": "employee_a",
        "name": "직원 A",
        "dept": "여신",
        "clearance": 2,
    },
    "employee_b": {
        "user_id": "employee_b",
        "name": "직원 B",
        "dept": "여신",
        "clearance": 1,
    },
    "employee_c": {
        "user_id": "employee_c",
        "name": "직원 C",
        "dept": "인사",
        "clearance": 2,
    },
}

DOCUMENTS = [
    {
        "id": "doc-001",
        "title": "상품 현황",
        "dept": "여신",
        "classification": 1,
        "content": (
            "연습용 개인신용대출 상품의 지난 분기 잔액은 100억 원이고, "
            "이번 분기 잔액은 110억 원이다."
        ),
    },
    {
        "id": "doc-002",
        "title": "내부 검토 의견",
        "dept": "여신",
        "classification": 2,
        "content": (
            "개인신용대출의 잔액 증가 원인을 추가 확인하고, "
            "보고서에 검토가 완료되지 않았음을 표시하도록 제안했다."
        ),
    },
    {
        "id": "doc-003",
        "title": "인사 현황",
        "dept": "인사",
        "classification": 1,
        "content": "연습용 인사 부서의 이번 분기 교육 참여 인원은 20명이다.",
    },
]

REPORT_REQUEST = {
    "period": "이번 분기",
    "subject": "개인신용대출",
    "audience": "여신 부서 내부",
    "goal": "대출 잔액 변화와 확인 가능한 검토 의견을 정리",
    "output_format": "현황, 변화율, 검토 의견, 근거 문서",
}

REPORT_PLAN = {
    "id": "report-plan-001",
    "steps": [
        {
            "id": "search",
            "capability": "document_search",
            "inputs": {
                "keyword": REPORT_REQUEST["subject"],
            },
        },
        {
            "id": "write",
            "capability": "report_writing",
            "inputs": {
                "documents" : {"from_step" : "search"},
            },
        },
    ],
}

REPORT_PROFILE = {
    "id": "report-profile-001",
    "version": 1,
    "allowed_capabilities": [
        "document_search",
        "report_writing",
    ],
}

# 다음 단계에서 실제 접근 검사 결과와 비교할 정답이다.
EXPECTED_DOCUMENT_IDS = {
    "employee_a": ["doc-001", "doc-002"],
    "employee_b": ["doc-001"],
    "employee_c": ["doc-003"],
}

def can_read_document(user, document):
    # 필수로 들어가는 값이 있는지
    if user.get("dept") is None or user.get("clearance") is None:
        return False

    if document.get("dept") is None or document.get("classification") is None:
        return False

    # 다른 부서의 문서는 거부
    if user["dept"] != document["dept"]:
        return False

    # 같은 부서라면 사용자 등급이 문서 등급 이상인지 확인
    return user["clearance"] >= document["classification"]

# 읽을 수 있는 서류만 가져오기
def get_readable_documents(user, documents):
    readable_documents = []

    for document in documents:
        if can_read_document(user, document=document):
            readable_documents.append(document)

    return readable_documents

# 서류 검색
def search_documents(user, documents, keyword):
    readable_documents = get_readable_documents(user,documents)
    matched_documents = []

    for doc in  readable_documents:
        text = doc["title"] + " " + doc["content"]

        if keyword in text:
            matched_documents.append(doc)
            
    return matched_documents

def document_search_worker(user, inputs):
    keyword = inputs["keyword"]

    # 기존 검색 함수를 호출한 결과를 반환하세요.
    return search_documents(user, DOCUMENTS , keyword)

def report_writing_worker(user, inputs):
    documents = inputs["documents"]

    if not documents:
        return {
            "status": "blocked",
            "reason": "접근 가능한 관련 근거 문서가 없습니다.",
        }

    return {
        "status": "completed",
        "content": "\n".join(
            document["content"] for document in documents
        ),
        "document_ids": [document["id"] for document in documents],
        "classification": max(document["classification"] for document in documents),
    }

def prepare_report(user, documents, keyword):
    found = search_documents(user, documents, keyword)

    if not found:
        return {
            "status": "blocked",
            "reason": "접근 가능한 관련 근거 문서가 없습니다.",
            "document_ids": [],
        }

    report_classification = max(document["classification"] for document in found)

    return {
        "status": "ready",
        "document_ids": [document["id"] for document in found],
        "classification" : report_classification,
    }

WORKER_REGISTRY = {
    "document_search": document_search_worker,
    "report_writing": report_writing_worker,
}    

def validate_plan(plan, profile):
    seen_step_ids = set()

    for step in plan["steps"]:
        step_id = step["id"]
        capability = step["capability"]

        if step_id in seen_step_ids:
            raise ValueError(f"중복된 단계 ID입니다: {step_id}")

        if capability not in WORKER_REGISTRY:
            raise ValueError(f"등록되지 않은 작업입니다: {capability}")

        if capability not in profile["allowed_capabilities"]:
            raise ValueError(f"프로필에 허용되지 않은 작업입니다: {capability}")

        for value in step["inputs"].values():
            if isinstance(value, dict) and "from_step" in value:
                source_step_id = value["from_step"]

                # 앞에 나온 단계인지 확인하세요.
                if source_step_id not in seen_step_ids:
                    raise ValueError(
                        f"앞선 단계만 참조할 수 있습니다: {source_step_id}"
                    )

        # 검사가 끝난 현재 단계 ID를 집합에 추가하세요.
        seen_step_ids.add(step_id)

def execute_step(user, step, inputs):
    capability = step["capability"]
    worker = WORKER_REGISTRY.get(capability)

    if worker is None:
        raise ValueError(f"등록되지 않은 작업 종류입니다:{capability}")

    # 선택된 worker에 user와 inputs를 전달해 실행하세요
    return worker(user, inputs)

def resolve_inputs(inputs, step_results):
    resolved = {}

    for name, value in inputs.items():
        if isinstance(value, dict) and "from_step" in value:
            source_step_id = value["from_step"]

            if source_step_id not in step_results:
                raise ValueError(
                    f"참조할 단계 결과가 없습니다: {source_step_id}"
                )

            # 이전 단계 결과를 resolved[name]에 넣으세요.
            resolved[name] = step_results[source_step_id]
        else:
            resolved[name] = value

    return resolved

def execute_plan(user, plan, *, profile=REPORT_PROFILE):
    validate_plan(plan, profile)
    step_results = {}

    for step in plan["steps"]:
        inputs = resolve_inputs(step["inputs"], step_results)
        result = execute_step(user, step, inputs)

        # 이번 단계의 ID를 키로 사용해 결과를 저장하세요.
        step_results[step["id"]] = result

        if isinstance(result, dict) and result.get("status") == "blocked":
            return {
                "status": "blocked",
                "step_results": step_results,
            }

    return {
        "status": "completed",
        "step_results": step_results,
    }

def save_execution(user, plan, execution, *, profile=REPORT_PROFILE):
    run_id = uuid4().hex

    record = {
        "run_id": run_id,
        "user_id": user["user_id"],
        "plan_id": plan["id"],
        "status": execution["status"],
        "step_results": execution["step_results"],
        "profile": {
            "id": profile["id"],
            "version": profile["version"],
            "allowed_capabilities": profile["allowed_capabilities"],
        },
        "plan": plan,
    }

    output_dir = Path("logs/report_runs")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{run_id}.json"

    # record를 JSON 문자열로 바꿔 파일에 저장하세요.
    output_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return output_path

def load_execution(path):
    text = Path(path).read_text(encoding= "utf-8")
    return json.loads(text)

def main():
    for user in USERS.values():
        execution = execute_plan(user, REPORT_PLAN)
        saved_path = save_execution(user, REPORT_PLAN, execution)
        loaded = load_execution(saved_path)

        print("프로필:", loaded["profile"]["id"])
        print("버전:", loaded["profile"]["version"])
        print("계획 단계:", [step["id"] for step in loaded["plan"]["steps"]])

        assert loaded["user_id"] == user["user_id"]
        assert loaded["plan"] == REPORT_PLAN
        assert loaded["step_results"] == execution["step_results"]


if __name__ == "__main__":
    main()