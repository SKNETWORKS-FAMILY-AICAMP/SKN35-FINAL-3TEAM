"""시장 DB — 금리·환율 수집 기록과 값 (UC-02-1, 2026-11-24)

수집기가 ECOS 에서 받은 파일을 받은 편지함(경계)에 놓으면, 적재기가 해시·형식을 검사해 통과한 값만 넣는다.
UC-02 리포트(market_snapshot, MARKET_DATA_MODE=db)는 이 표의 accepted 값만 쓴다.

- market_fetch  받은 기록 한 번 = 한 줄. 성공·빈 응답·오류·거부 모두 남긴다 (계열별 결과는 detail).
- market_value  계열·관측일별 값.
                accepted   = 리포트에 쓰는 값 (계열·관측일마다 하나)
                held       = 급변 값 보류 (EXT-1: 환율이 직전 관측보다 3% 넘게, 기준금리가 0.5%p 넘게 움직임)
                superseded = 정정 전 값. 기존 값은 고치지 않고 정정 표시만 한다

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-24
"""
from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE market_fetch (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source           VARCHAR(20)  NOT NULL CHECK (source IN ('live', 'replay')),
    requested_start  DATE         NOT NULL,
    requested_end    DATE         NOT NULL,
    file_path        VARCHAR(500),
    sha256           CHAR(64),
    status           VARCHAR(20)  NOT NULL CHECK (status IN ('loading', 'loaded', 'rejected', 'error')),
    detail           JSONB        NOT NULL DEFAULT '{}',
    error            TEXT,
    fetched_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    loaded_at        TIMESTAMPTZ
)""")
    op.execute("CREATE INDEX ix_market_fetch_time ON market_fetch (fetched_at DESC)")
    op.execute("COMMENT ON TABLE market_fetch IS '시장 데이터 받은 기록 (UC-02-1). 성공·빈 응답·오류·거부 모두'")
    op.execute("COMMENT ON COLUMN market_fetch.detail IS '계열별 결과 {code: {status: ok|empty|error, accepted, held, corrected, unchanged}}'")

    op.execute("""
CREATE TABLE market_value (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    series_code    VARCHAR(20)    NOT NULL,
    obs_date       DATE           NOT NULL,
    value          NUMERIC(20, 6) NOT NULL,
    status         VARCHAR(20)    NOT NULL CHECK (status IN ('accepted', 'held', 'superseded')),
    fetch_id       BIGINT         NOT NULL REFERENCES market_fetch (id),
    note           TEXT,
    created_at     TIMESTAMPTZ    NOT NULL DEFAULT now(),
    superseded_at  TIMESTAMPTZ
)""")
    op.execute("CREATE UNIQUE INDEX ux_market_value_accepted ON market_value (series_code, obs_date) WHERE status = 'accepted'")
    op.execute("CREATE INDEX ix_market_value_series ON market_value (series_code, obs_date)")
    op.execute("COMMENT ON TABLE market_value IS '금리·환율 값 (UC-02-1). 리포트는 accepted 만 쓴다'")
    op.execute("COMMENT ON COLUMN market_value.status IS 'accepted = 사용, held = 급변 보류(EXT-1), superseded = 정정 전 값'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS market_value")
    op.execute("DROP TABLE IF EXISTS market_fetch")
