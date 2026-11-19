from dataclasses import dataclass
from typing import Iterable

from apps.api.src.harness.dy.agent import Agent
from apps.api.src.harness.dy.tools.registry import SimpleToolRegistry
from contracts.harness import LLMClient, Tool
from contracts.permission import ApprovalHandler, AuditLogger, PermissionChecker


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    system_prompt: str
    tool_names: tuple[str, ...]


def create_agent(
    config: AgentConfig,
    available_tools: Iterable[Tool],
    *,
    llm: LLMClient,
    checker: PermissionChecker,
    approver: ApprovalHandler,
    audit: AuditLogger,
) -> Agent:
    catalog = {tool.spec.name: tool for tool in available_tools}
    unknown_tools = [name for name in config.tool_names if name not in catalog]
    if unknown_tools:
        raise ValueError(
            f"Agent {config.agent_id} has unknown tools: {', '.join(unknown_tools)}"
        )

    return Agent(
        llm=llm,
        registry=SimpleToolRegistry(catalog[name] for name in config.tool_names),
        checker=checker,
        approver=approver,
        audit=audit,
        system_prompt=config.system_prompt,
    )
