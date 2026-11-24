"""알림함·고객 안내 발송 기록, 산출물 공개 번호 (2026-11-24)

- notification     직원 알림함 (UC-03 만기 알림, UC-05 발송 결과). 받는 사람만 연다.
                   지울 때는 deleted_at 만 채운다 (목록에서만 빠짐).
- customer_notice  고객 안내 메일 발송 기록 (UC-05). 같은 대출·같은 만기일 안내는 한 번만.
                   승인 전에 'sending' 으로 자리를 잡고, 발송 뒤 'sent'. 실패는 'failed' (다시 보낼 수 있음),
                   반려는 'rejected' (다시 요청할 수 있음). 응답이 끊겨 결과를 모르면 'sending' 그대로 둔다 (다시 보내지 않음).
- output_file.public_id  화면·툴이 쓰는 문서 번호 (32자리 16진수). 순번(id)을 밖에 보이지 않게 한다.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-24
"""
from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE notification (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id            BIGINT       NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind               VARCHAR(30)  NOT NULL CHECK (kind IN ('maturity_alert', 'notice_result')),
    title              VARCHAR(200) NOT NULL,
    body               TEXT         NOT NULL DEFAULT '',
    body_dropped       BOOLEAN      NOT NULL DEFAULT false,
    payload            JSONB        NOT NULL DEFAULT '{}',
    has_customer_info  BOOLEAN      NOT NULL DEFAULT false,
    session_id         UUID REFERENCES chat_session (id) ON DELETE SET NULL,
    run_id             UUID REFERENCES agent_run (id) ON DELETE SET NULL,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    read_at            TIMESTAMPTZ,
    deleted_at         TIMESTAMPTZ
)""")
    op.execute("CREATE INDEX ix_notification_user ON notification (user_id, created_at DESC) WHERE deleted_at IS NULL")
    op.execute("COMMENT ON TABLE notification IS '직원 알림함. 받는 사람만 조회 (UC-03 만기 알림, UC-05 발송 결과)'")
    for column, text in {
        "kind": "maturity_alert = 만기 알림, notice_result = 고객 안내 발송·반려 결과",
        "body": "모델이 쓴 안내 문장 (숫자 검사를 통과한 것만)",
        "body_dropped": "안내 문장에 조회 결과에 없는 숫자가 있어 뺐는지",
        "payload": "보낼 때의 목록 (기준일·건수·이름을 가린 고객 목록)",
        "read_at": "처음 연 시각. NULL 이면 안 읽음",
        "deleted_at": "직원이 지운 시각 (목록에서만 빠짐)",
    }.items():
        op.execute(f"COMMENT ON COLUMN notification.{column} IS '{text}'")

    op.execute("""
CREATE TABLE customer_notice (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        BIGINT       NOT NULL REFERENCES users (id),
    loan_id        VARCHAR(50)  NOT NULL,
    maturity_date  DATE         NOT NULL,
    recipient      VARCHAR(255) NOT NULL,
    subject        VARCHAR(255) NOT NULL,
    status         VARCHAR(20)  NOT NULL DEFAULT 'sending'
                   CHECK (status IN ('sending', 'sent', 'failed', 'rejected')),
    message_id     VARCHAR(100),
    call_id        VARCHAR(100),
    error          TEXT,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    sent_at        TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
)""")
    # 보냈거나 보내는 중인 안내는 대출·만기일마다 한 건만 (실패·반려는 여러 건 남을 수 있음)
    op.execute("""CREATE UNIQUE INDEX ux_customer_notice_once ON customer_notice (loan_id, maturity_date)
                  WHERE status IN ('sending', 'sent')""")
    op.execute("CREATE INDEX ix_customer_notice_user ON customer_notice (user_id, created_at DESC)")
    op.execute("COMMENT ON TABLE customer_notice IS '고객 만기 안내 메일 발송 기록 (UC-05). 같은 대출·만기일은 한 번만 발송'")
    for column, text in {
        "loan_id": "여신 시스템의 대출 번호",
        "maturity_date": "안내한 만기일. 만기가 연장되면 새 만기일로 다시 보낼 수 있다",
        "status": "sending = 보내는 중(결과 모름 포함), sent = 보냄, failed = 실패, rejected = 승인 단계에서 반려",
        "message_id": "Gmail 메시지 ID (보냈을 때)",
    }.items():
        op.execute(f"COMMENT ON COLUMN customer_notice.{column} IS '{text}'")

    op.execute("ALTER TABLE output_file ADD COLUMN public_id CHAR(32) UNIQUE")
    op.execute("ALTER TABLE output_file ADD COLUMN kind VARCHAR(30) NOT NULL DEFAULT 'form'")
    op.execute("COMMENT ON COLUMN output_file.public_id IS '화면·툴이 쓰는 문서 번호 (32자리 16진수)'")
    op.execute("COMMENT ON COLUMN output_file.kind IS 'form = 양식 문서, market_report = 금리·환율 리포트'")


def downgrade() -> None:
    op.execute("ALTER TABLE output_file DROP COLUMN IF EXISTS kind")
    op.execute("ALTER TABLE output_file DROP COLUMN IF EXISTS public_id")
    op.execute("DROP TABLE IF EXISTS customer_notice")
    op.execute("DROP TABLE IF EXISTS notification")
