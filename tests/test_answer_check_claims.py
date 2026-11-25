"""완료 주장 검사: 쓰기·발송 툴이 성공하지 않았는데 보냈다고 하는 답."""

import pytest

from harness.answer_check import false_completion


@pytest.mark.parametrize("answer", [
    "성공: 메일 발송 준비 완료 (시스템에서 자동 발송)",
    "메일 전송 완료",
    "고객에게 보냈습니다.",
])
def test_send_claims_without_a_send_are_caught(answer):
    assert false_completion(answer, wrote=False)
    assert not false_completion(answer, wrote=True)


# 메일 내용을 설명하는 답("이 메일은 어제 발송되었습니다")은 완료 주장으로 보지 않는다
@pytest.mark.parametrize("answer", ["이대로 보낼까요?", "아직 보내지 않았습니다.", "메일을 보내지 못했습니다.",
                                    "이 메일은 어제 발송되었습니다."])
def test_questions_and_not_sent_are_fine(answer):
    assert not false_completion(answer, wrote=False)


def test_rejection_reply_does_not_dump_json_results():
    """2026-11-25: 승인 거절 안내의 '완료된 작업'에 안내문 준비 결과(고객 이메일·본문 JSON)가 그대로 나왔다."""
    import json

    from contracts.harness import Denial
    from harness.denial import DoneTool, template_reply

    prepared = json.dumps({"notices": [{"loan_id": "L-1", "customer": "홍*동", "to": ["a@example.com"], "body": "고객님"}]})
    text = template_reply(Denial("c1", "send_maturity_notice", "rejected", "승인 거절", "만기 안내 메일 보내기"),
                          [DoneTool("만기 안내문 준비", prepared, "prepare_maturity_notices")])
    assert "보낼 고객 1명 확인" in text and "a@example.com" not in text and "{" not in text
