from apps.api.src.harness.dy.agents.factory import AgentConfig, create_agent
from apps.api.src.harness.dy.tools.arithmetic import AddTool, SubtractTool, MultiplyTool

MATH_TOOLS = (AddTool, SubtractTool, MultiplyTool)
MATH_AGENT = AgentConfig(
    agent_id="math",
    system_prompt="당신은 수학 Agent입니다. 계산에는 반드시 해당하는 Tool을 사용하세요.",
    tool_names=("add", "subtract", "multiply"),
)


def create_math_agent(llm, checker, approver, audit):
    return create_agent(
        MATH_AGENT, MATH_TOOLS, llm=llm, checker=checker,
        approver=approver, audit=audit,
    )
