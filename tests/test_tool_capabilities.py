"""매 요청의 모델 입력과 도구 목록이 에이전트 설정을 일관되게 반영하는지 확인한다."""

from services.chat import tool_capability_prompt
from tests.test_agents import BODY, KIM, h, make
from tools.today import TodayTool


def capability_section(message):
    return message.content.split("[현재 제공된 도구]\n", 1)[1].split("[기능 사용 원칙]", 1)[0]


async def test_capabilities_follow_tool_addition_and_removal_in_same_session(tmp_path):
    client, svc = make([{"content": "응답"}] * 3, tmp_path)
    svc.tools.register(TodayTool())
    async with client:
        created = await client.post("/api/agents", headers=h(KIM), json=BODY)
        aid = created.json()["agent_id"]
        first = await client.post("/api/chat/messages", headers=h(KIM), json={"agent_id": aid, "content": "오늘 날짜는?"})
        assert first.status_code == 200
        sid = first.json()["session_id"]
        assert "today (" not in capability_section(svc.llm.calls[-1][0][0])
        assert "현재 날짜 확인 도구는 제공되지 않았다" in svc.llm.calls[-1][0][0].content

        for names in (["add", "today"], ["add"]):
            patched = await client.patch(f"/api/agents/{aid}", headers=h(KIM), json={"tools": names})
            assert patched.status_code == 200
            response = await client.post("/api/chat/messages", headers=h(KIM), json={"session_id": sid, "content": "오늘 날짜는?"})
            assert response.status_code == 200
            messages, specs = svc.llm.calls[-1]
            assert [spec.name for spec in specs] == names
            assert ("today (" in capability_section(messages[0])) == ("today" in names)
            assert ("현재 날짜 확인 도구는 제공되지 않았다" in messages[0].content) == ("today" not in names)
            assert sum(message.role == "system" for message in messages) == 1


async def test_agent_without_tools_still_calls_model_with_general_knowledge_guidance(tmp_path):
    client, svc = make([{"content": "영업일은 업무를 하는 날입니다."}], tmp_path)
    async with client:
        aid = (await client.post("/api/agents", headers=h(KIM), json={**BODY, "tools": []})).json()["agent_id"]
        response = await client.post("/api/chat/messages", headers=h(KIM), json={"agent_id": aid, "content": "영업일이 뭐야?"})
        assert response.status_code == 200
        assert response.json()["tool_events"] == []
        messages, specs = svc.llm.calls[0]
        assert specs == []
        assert "제공된 도구 없음" in capability_section(messages[0])
        assert "일반 지식의 설명" in messages[0].content


async def test_default_agent_sees_all_registered_tools(tmp_path):
    client, svc = make([{"content": "안녕하세요"}], tmp_path)
    svc.tools.register(TodayTool())
    async with client:
        response = await client.post("/api/chat/messages", headers=h(KIM), json={"content": "사용 가능한 기능은?"})
        assert response.status_code == 200
        messages, specs = svc.llm.calls[0]
        for spec in specs:
            assert f"- {spec.name} ({spec.label}):" in capability_section(messages[0])


def test_capability_rules_distinguish_tool_availability_from_permission():
    prompt = tool_capability_prompt([])
    assert "사용자 권한 거부와 구분" in prompt
    assert "실행 시 권한 검사와 승인" in prompt
