from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from apps.api.src.harness.dy.agents.math_agents import create_math_agent
from apps.api.src.harness.dy.audit import JsonlAuditLogger
from apps.api.src.harness.dy.local_math_client import LocalMathClient
from apps.api.src.harness.dy.session import Session
from contracts.context import RunContext
from contracts.harness import RunResult
from contracts.permission import AlwaysAllowChecker
from demo import NoApprovalNeeded

def run_local_demo(content: str)->RunResult:
    ctx = RunContext(
        run_id=str(uuid4()),
        session_id=str(uuid4()),
        user_id="local-demo",
        roles=("user",),
        data_level=0,
        agent_id="math",
    )

    with TemporaryDirectory() as directory:
        agent = create_math_agent(
            LocalMathClient(),
            AlwaysAllowChecker(),
            NoApprovalNeeded(),
            JsonlAuditLogger(Path(directory) / "audit.jsonl"),
        )
        session = Session(ctx.session_id, storage_dir=directory)
        return agent.run(ctx, session, content)
