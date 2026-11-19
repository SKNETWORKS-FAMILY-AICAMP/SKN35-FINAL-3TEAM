"""Run from the repository root with python -m apps.api.src.harness.dy.agent_setup_demo."""

from argparse import ArgumentParser
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from apps.api.src.harness.dy.agents.factory import AgentConfig, create_agent
from apps.api.src.harness.dy.agents.math_agents import MATH_AGENT, MATH_TOOLS
from apps.api.src.harness.dy.approval import CliApprovalHandler
from apps.api.src.harness.dy.audit import JsonlAuditLogger
from apps.api.src.harness.dy.local_math_client import LocalMathClient
from apps.api.src.harness.dy.session import Session
from contracts.context import RunContext
from contracts.permission import AlwaysAllowChecker


ADD_ONLY_AGENT = AgentConfig(
    agent_id="add-only",
    system_prompt="당신은 덧셈을 담당하는 에이전트입니다.",
    tool_names=("add",),
)


def main() -> None:
    parser = ArgumentParser(description="Compare agents with different tools.")
    parser.add_argument("question", nargs="?", default="7 * 8")
    args = parser.parse_args()

    with TemporaryDirectory() as directory:
        audit = JsonlAuditLogger(Path(directory) / "audit.jsonl")
        for config in (MATH_AGENT, ADD_ONLY_AGENT):
            agent = create_agent(
                config,
                MATH_TOOLS,
                llm=LocalMathClient(),
                checker=AlwaysAllowChecker(),
                approver=CliApprovalHandler(),
                audit=audit,
            )
            ctx = RunContext(
                run_id=str(uuid4()),
                session_id=str(uuid4()),
                user_id="local-demo",
                roles=("user",),
                data_level=0,
                agent_id=config.agent_id,
            )
            session = Session(ctx.session_id, storage_dir=directory)
            result = agent.run(ctx, session, args.question)
            tools = ", ".join(spec.name for spec in agent.registry.specs())
            print(f"[{config.agent_id}] tools: {tools}")
            for message in session.get_history():
                if message.role == "tool":
                    print(f"  tool result: {message.content}")
            print(f"  answer: {result.message}")


if __name__ == "__main__":
    main()
