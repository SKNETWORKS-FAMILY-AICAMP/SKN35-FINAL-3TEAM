from apps.api.src.harness.dy.agent import Agent
from apps.api.src.harness.dy.tools.arithmetic import AddTool, SubtractTool, MultiplyTool
from apps.api.src.harness.dy.tools.registry import SimpleToolRegistry

def create_math_agent(llm, checker, approver, audit):
    return Agent(llm=llm, checker=checker, approver=approver, audit=audit,
                 registry=SimpleToolRegistry([AddTool, SubtractTool, MultiplyTool]),
                 system_prompt="당신은 수학 Agent입니다. 계산에는 반드시 해당하는 Tool을 사용하세요.")