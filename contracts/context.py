# 누가 어떤 권한으로 어떤 Agent를 실행했는지

from dataclasses import dataclass

# 생성한 뒤에는 변경 X
@dataclass(frozen=True)

class RunContext:
    run_id:str      # 실행 번호
    session_id:str  # 어떤 세션에서 실행 중인지

    user_id:str     # 실행 요청 사용자
    roles: tuple[str, ...]  # RBAC 역할
    data_level:int  # 사용자 데이터 접근 레벨
    
    agent_id:str    # 어떤 에이전트로 실행 중인지
    parent_run_id:str | None = None     # 누가 나를 호출했는지
    depth:int=0     # 서브에이전트 깊이