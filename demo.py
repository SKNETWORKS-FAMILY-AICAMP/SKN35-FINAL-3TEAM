"""LLM 서버 없이 하네스 전체 흐름을 시험하는 작은 예제."""
import tempfile
import re
import json
from pathlib import Path
from apps.api.src.harness.dy.llm_client import OpenAICompatClient
from contracts.context import RunContext
from contracts.harness import LLMResponse, LLMToolCall, Message
from contracts.permission import AlwaysAllowChecker
from apps.api.src.harness.dy.agents.math_agents import create_math_agent
from apps.api.src.harness.dy.audit import JsonlAuditLogger
from apps.api.src.harness.dy.session import Session
from apps.api.src.harness.dy.math_permission import MathPermissionChecker
from apps.api.src.harness.dy.approval import CliApprovalHandler

class DemoLLM:
    def chat(self, messages, tools):
        latest = messages[-1]

        if latest.role == "user":
            call = LLMToolCall(
                call_id="demo-add",
                tool_name="add",
                arguments={"a": 10, "b": 5},
            )
        elif latest.role == "tool" and latest.tool_call_id == "demo-add":
            call = LLMToolCall(
                call_id="demo-multiply",
                tool_name="multiply",
                arguments={"a": int(latest.content), "b": 2},
            )
        elif latest.role == "tool" and latest.tool_call_id == "demo-multiply":
            return LLMResponse(
                Message(role="assistant", content=f"계산 결과는 {latest.content}입니다.")
            )
        else:
            raise RuntimeError(f"예상하지 못한 메시지: {latest}")

        return LLMResponse(Message(role="assistant", tool_calls=(call,)))

class NoApprovalNeeded:
    def request_approval(self, call, result):
        return False

def main():
    with tempfile.TemporaryDirectory() as tmp:
        # 올라마
        # llm = OpenAICompatClient(
        #     model="llama3.1",
        #     base_url="http://localhost:11434/v1",
        #     temperature=0
        # )
        llm = DemoLLM()
        common = (
            llm,
            MathPermissionChecker(),
            CliApprovalHandler(),
            JsonlAuditLogger(Path(tmp) / "audit.jsonl") # 저장 X
            # JsonlAuditLogger("logs/audit.jsonl") # 저장 O
        )

        # 세션
        # session = Session(sid, storage_dir=tmp) # 세선 저장 X
        session = Session("math-session",storage_dir="sessions") # 세션 저장 O

        # 여러 질문 준비(권한 확인)
        # questions = [
        #     "10과 5를 더해줘",
        #     "10에서 5를 빼줘",
        #     "7과 8을 곱해줘",
        #     "100에서 40을 빼줘"
        # ]
        # 여러 질문 준비(세션 저장)
        # questions = [
        #      "10과 5를 더해줘",
        #     "방금 결과에서 3을 빼줘",
        #     "이번 결과에 2를 곱해줘"
        # ]
        questions = [
            "7과 8을 multiply 도구로 곱해줘"
        ]
        for index, (name, factory) in enumerate([
            ("수학", create_math_agent)
        ]):
            agent = factory(*common)
            sid = f"example-{index}"
            ctx = RunContext(run_id=sid,session_id=sid,
                user_id="user",roles=("user",), # admin , user
                data_level=0,agent_id=name
            )
            # 질문을 하나씩 실행
            for question in questions:
                result = agent.run(ctx, session, question)
                print(f"질문: {question}")
                print(f"{name} Agent → [{result.status}] {result.message}")
                print("--------------------")

        # show_logs() # 로그 보여주기

def show_logs():
    path = Path("logs/audit.jsonl")

    if not path.exists():
        print("아직 실행 기록이 없습니다.")
        return

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)

            call = record["call"]
            permission = record["result"]

            print("------------------")
            print("사용자:", call["principal"]["user_id"])
            print("Tool:", call["tool_name"])
            print("권한:", permission["decision"])
            print("실행:", record["executed"])

if __name__ == "__main__":
    main()
