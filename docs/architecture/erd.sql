-- 업무 자동화 에이전트 ERD 최종본 (2026-11-18, ERDCloud 내보내기 기준)
-- 수정: CopyOfagent → agent_run 정리, message.id2/id3 → session_id/run_id,
--       ERDCloud 내보내기에서 빠진 비식별 관계 FK 를 파일 끝에 추가
CREATE TABLE `department` (
	`id`	BIGINT	NOT NULL	COMMENT '부서 ID',
	`name`	VARCHAR(100)	NOT NULL	COMMENT '부서명',
	`head_user_id`	BIGINT	NULL	COMMENT '부서장 사용자 ID'
);

CREATE TABLE `users` (
	`id`	BIGINT	NOT NULL	COMMENT '사용자 ID',
	`login_id`	VARCHAR(100)	NOT NULL	COMMENT '로그인 ID',
	`name`	VARCHAR(100)	NOT NULL	COMMENT '이름',
	`dept_id`	BIGINT	NOT NULL	COMMENT '소속 부서 ID',
	`position`	VARCHAR(50)	NULL	COMMENT '직급',
	`security_grade`	INT	NOT NULL	DEFAULT 1	COMMENT '보안등급'
);

CREATE TABLE `user_role` (
	`role`	VARCHAR(50)	NOT NULL	COMMENT '역할 (admin, dept_head, member)',
	`user_id`	BIGINT	NOT NULL	COMMENT '사용자 ID'
);

CREATE TABLE `agent` (
	`id`	BIGINT	NOT NULL	COMMENT '에이전트 ID',
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '만든 사용자 ID',
	`name`	VARCHAR(100)	NOT NULL	COMMENT '에이전트 이름',
	`description`	TEXT	NULL	COMMENT '설명',
	`system_prompt`	TEXT	NOT NULL	COMMENT '시스템 프롬프트',
	`scope`	VARCHAR(20)	NOT NULL	DEFAULT 'personal'	COMMENT '공개 범위 (personal, dept, company)',
	`status`	VARCHAR(20)	NOT NULL	DEFAULT 'active'	COMMENT '상태 (active, disabled)'
);

CREATE TABLE `agent_tool` (
	`tool_name`	VARCHAR(100)	NOT NULL	COMMENT '사용할 수 있는 툴 이름 (실행 시 권한은 실행자 기준으로 따로 판정)',
	`agent_id`	BIGINT	NOT NULL	COMMENT '에이전트 ID'
);

CREATE TABLE `agent_share_request` (
	`id`	BIGINT	NOT NULL	COMMENT '공유 요청 ID',
	`agent_id`	BIGINT	NOT NULL	COMMENT '에이전트 ID',
	`requester_id`	BIGINT	NOT NULL	COMMENT '요청한 사용자 ID',
	`approver_id`	BIGINT	NOT NULL	COMMENT '승인할 사용자 ID (바로 위 관리자)',
	`requested_scope`	VARCHAR(20)	NOT NULL	COMMENT '요청 공개 범위 (dept, company)',
	`status`	VARCHAR(20)	NOT NULL	DEFAULT 'pending'	COMMENT '상태 (pending, approved, rejected)',
	`comment`	TEXT	NULL	COMMENT '승인·반려 의견',
	`requested_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '요청 시각',
	`decided_at`	DATETIME	NULL	COMMENT '처리 시각'
);

CREATE TABLE `chat_session` (
	`id`	CHAR(36)	NOT NULL	COMMENT '세션 ID (UUID)',
	`user_id`	BIGINT	NOT NULL	COMMENT '사용자 ID',
	`agent_id`	BIGINT	NULL	COMMENT '사용한 에이전트 ID (NULL 이면 기본 에이전트)',
	`title`	VARCHAR(200)	NULL	COMMENT '세션 제목',
	`updated_at`	DATETIME	NULL	COMMENT '마지막 대화 시각'
);

CREATE TABLE `message` (
	`id`	BIGINT	NOT NULL	COMMENT '메시지 ID',
	`seq`	INT	NOT NULL	COMMENT '세션 내 순서',
	`role`	VARCHAR(20)	NOT NULL	COMMENT '역할 (system, user, assistant, tool)',
	`content`	TEXT	NULL	COMMENT '내용',
	`tool_call_id`	VARCHAR(100)	NULL	COMMENT 'role=tool 일 때 대응 툴 호출 ID',
	`session_id`	CHAR(36)	NOT NULL	COMMENT '세션 ID',
	`run_id`	CHAR(36)	NULL	COMMENT '실행 ID (이 메시지를 만든 요청)'
);

CREATE TABLE `session_document` (
	`session_id`	CHAR(36)	NOT NULL	COMMENT '세션 ID',
	`document_id`	BIGINT	NOT NULL	COMMENT '첨부한 문서 ID'
);

CREATE TABLE `tool_call` (
	`call_id`	VARCHAR(100)	NOT NULL	COMMENT '툴 호출 ID (LLM 이 준 id)',
	`message_id`	BIGINT	NOT NULL	COMMENT '요청한 assistant 메시지 ID',
	`seq`	INT	NOT NULL	COMMENT '응답 내 호출 순서',
	`tool_name`	VARCHAR(100)	NOT NULL	COMMENT '툴 이름',
	`arguments`	JSON	NULL	COMMENT '툴 인자',
	`ok`	BOOLEAN	NULL	COMMENT '성공 여부',
	`result_content`	TEXT	NULL	COMMENT 'LLM 에게 돌려준 결과',
	`duration_ms`	INT	NULL	COMMENT '실행 시간(ms)'
);

CREATE TABLE `approval_request` (
	`id`	BIGINT	NOT NULL	COMMENT '승인 요청 ID',
	`call_id`	VARCHAR(100)	NOT NULL	COMMENT '툴 호출 ID',
	`status`	VARCHAR(20)	NOT NULL	DEFAULT 'pending'	COMMENT '상태 (pending, approved, rejected, timeout)',
	`reason`	TEXT	NOT NULL	COMMENT '승인 요청 사유'
);

CREATE TABLE `policy_rule` (
	`policy_rule_id`	VARCHAR(100)	NOT NULL	COMMENT '규칙 ID',
	`priority`	INT	NOT NULL	COMMENT '매칭 순서 (작을수록 먼저)',
	`tool`	VARCHAR(100)	NOT NULL	COMMENT '툴 이름 또는 *',
	`decision`	VARCHAR(10)	NOT NULL	COMMENT '판정 (allow, deny, ask)',
	`conditions`	JSON	NULL	COMMENT 'ABAC 조건 (사용자 속성, 툴 인자)',
	`reason`	TEXT	NULL	COMMENT '사유 (deny, ask 일 때 필수)',
	`enabled`	BOOLEAN	NOT NULL	DEFAULT TRUE	COMMENT '사용 여부'
);

CREATE TABLE `audit_log` (
	`id`	BIGINT	NOT NULL	COMMENT '감사 로그 ID',
	`call_id`	VARCHAR(100)	NOT NULL	COMMENT '툴 호출 ID',
	`session_id`	CHAR(36)	NOT NULL	COMMENT '세션 ID',
	`user_id`	BIGINT	NOT NULL	COMMENT '요청자 ID',
	`agent_id`	BIGINT	NULL	COMMENT '호출한 에이전트 ID (기록용, FK 없음)',
	`tool_name`	VARCHAR(100)	NOT NULL	COMMENT '툴 이름',
	`arguments`	JSON	NULL	COMMENT '툴 인자',
	`principal_snapshot`	JSON	NULL	COMMENT '판정 시점 요청자 속성',
	`decision`	VARCHAR(10)	NOT NULL	COMMENT '판정 (allow, deny, ask)',
	`reason`	TEXT	NULL	COMMENT '판정 사유',
	`rule_id`	VARCHAR(100)	NULL	COMMENT '매칭된 규칙 ID (없으면 기본값)',
	`approval`	VARCHAR(20)	NULL	COMMENT 'ASK 일 때 승인 결과 (approved, rejected, timeout)',
	`executed`	BOOLEAN	NOT NULL	DEFAULT FALSE	COMMENT '실행 여부',
	`tool_ok`	BOOLEAN	NULL	COMMENT '실행했을 때 툴 성공 여부 (실행 안 했으면 NULL)',
	`ts`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '판정 시각 (UTC)'
);

CREATE TABLE `document` (
	`id`	BIGINT	NOT NULL	COMMENT '문서 ID',
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '업로드한 사용자 ID',
	`owner_dept_id`	BIGINT	NOT NULL	COMMENT '소유 부서 ID',
	`title`	VARCHAR(255)	NOT NULL	COMMENT '문서명',
	`scope`	VARCHAR(20)	NOT NULL	COMMENT '공개 범위 (personal, dept, company)',
	`security_grade`	INT	NOT NULL	DEFAULT 1	COMMENT '보안등급'
);

CREATE TABLE `document_version` (
	`id`	BIGINT	NOT NULL	COMMENT '문서 버전 ID',
	`document_id`	BIGINT	NOT NULL	COMMENT '문서 ID',
	`version`	INT	NOT NULL	COMMENT '버전 번호',
	`is_current`	BOOLEAN	NOT NULL	DEFAULT TRUE	COMMENT '현재 버전 여부',
	`file_name`	VARCHAR(255)	NOT NULL	COMMENT '원본 파일명',
	`mime_type`	VARCHAR(100)	NOT NULL	COMMENT '파일 형식',
	`storage_path`	VARCHAR(500)	NOT NULL	COMMENT '저장 경로',
	`status`	VARCHAR(20)	NOT NULL	DEFAULT 'processing'	COMMENT '처리 상태 (processing, ready, failed)'
);

CREATE TABLE `chunk` (
	`id`	BIGINT	NOT NULL	COMMENT '청크 ID',
	`document_version_id`	BIGINT	NOT NULL	COMMENT '문서 버전 ID',
	`chunk_index`	INT	NOT NULL	COMMENT '청크 순번',
	`page`	INT	NULL	COMMENT '페이지',
	`content`	TEXT	NOT NULL	COMMENT '청크 본문',
	`owner_dept_id`	BIGINT	NOT NULL	COMMENT '소유 부서 ID (문서에서 상속)',
	`scope`	VARCHAR(20)	NOT NULL	COMMENT '공개 범위 (문서에서 상속)',
	`security_grade`	INT	NOT NULL	COMMENT '보안등급 (문서에서 상속)',
	`vector_id`	VARCHAR(100)	NULL	COMMENT '벡터 DB 키'
);

CREATE TABLE `citation` (
	`message_id`	BIGINT	NOT NULL	COMMENT '답변 메시지 ID',
	`chunk_id`	BIGINT	NOT NULL	COMMENT '근거 청크 ID',
	`score`	FLOAT	NULL	COMMENT '검색 유사도'
);

CREATE TABLE `agent_run` (
	`id`	CHAR(36)	NOT NULL	COMMENT '실행 ID (UUID)',
	`session_id`	CHAR(36)	NOT NULL	COMMENT '세션 ID',
	`llm_calls`	INT	NOT NULL	DEFAULT 0	COMMENT 'LLM 호출 횟수',
	`stop_reason`	VARCHAR(20)	NULL	COMMENT '종료 사유 (completed, max_turns, denied)',
	`started_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '시작 시각',
	`ended_at`	DATETIME	NULL	COMMENT '종료 시각 (실행 중이면 NULL)'
);

ALTER TABLE `department` ADD CONSTRAINT `PK_DEPARTMENT` PRIMARY KEY (
	`id`
);

ALTER TABLE `users` ADD CONSTRAINT `PK_USERS` PRIMARY KEY (
	`id`
);

ALTER TABLE `user_role` ADD CONSTRAINT `PK_USER_ROLE` PRIMARY KEY (
	`role`,
	`user_id`
);

ALTER TABLE `agent` ADD CONSTRAINT `PK_AGENT` PRIMARY KEY (
	`id`
);

ALTER TABLE `agent_tool` ADD CONSTRAINT `PK_AGENT_TOOL` PRIMARY KEY (
	`tool_name`,
	`agent_id`
);

ALTER TABLE `agent_share_request` ADD CONSTRAINT `PK_AGENT_SHARE_REQUEST` PRIMARY KEY (
	`id`
);

ALTER TABLE `chat_session` ADD CONSTRAINT `PK_CHAT_SESSION` PRIMARY KEY (
	`id`
);

ALTER TABLE `message` ADD CONSTRAINT `PK_MESSAGE` PRIMARY KEY (
	`id`
);

ALTER TABLE `session_document` ADD CONSTRAINT `PK_SESSION_DOCUMENT` PRIMARY KEY (
	`session_id`,
	`document_id`
);

ALTER TABLE `tool_call` ADD CONSTRAINT `PK_TOOL_CALL` PRIMARY KEY (
	`call_id`
);

ALTER TABLE `approval_request` ADD CONSTRAINT `PK_APPROVAL_REQUEST` PRIMARY KEY (
	`id`
);

ALTER TABLE `policy_rule` ADD CONSTRAINT `PK_POLICY_RULE` PRIMARY KEY (
	`policy_rule_id`
);

ALTER TABLE `audit_log` ADD CONSTRAINT `PK_AUDIT_LOG` PRIMARY KEY (
	`id`
);

ALTER TABLE `document` ADD CONSTRAINT `PK_DOCUMENT` PRIMARY KEY (
	`id`
);

ALTER TABLE `document_version` ADD CONSTRAINT `PK_DOCUMENT_VERSION` PRIMARY KEY (
	`id`
);

ALTER TABLE `chunk` ADD CONSTRAINT `PK_CHUNK` PRIMARY KEY (
	`id`
);

ALTER TABLE `citation` ADD CONSTRAINT `PK_CITATION` PRIMARY KEY (
	`message_id`,
	`chunk_id`
);

ALTER TABLE `agent_run` ADD CONSTRAINT `PK_AGENT_RUN` PRIMARY KEY (
	`id`
);

ALTER TABLE `user_role` ADD CONSTRAINT `FK_users_TO_user_role_1` FOREIGN KEY (
	`user_id`
)
REFERENCES `users` (
	`id`
);

ALTER TABLE `agent_tool` ADD CONSTRAINT `FK_agent_TO_agent_tool_1` FOREIGN KEY (
	`agent_id`
)
REFERENCES `agent` (
	`id`
);

ALTER TABLE `session_document` ADD CONSTRAINT `FK_chat_session_TO_session_document_1` FOREIGN KEY (
	`session_id`
)
REFERENCES `chat_session` (
	`id`
);

ALTER TABLE `session_document` ADD CONSTRAINT `FK_document_TO_session_document_1` FOREIGN KEY (
	`document_id`
)
REFERENCES `document` (
	`id`
);

ALTER TABLE `citation` ADD CONSTRAINT `FK_message_TO_citation_1` FOREIGN KEY (
	`message_id`
)
REFERENCES `message` (
	`id`
);

ALTER TABLE `citation` ADD CONSTRAINT `FK_chunk_TO_citation_1` FOREIGN KEY (
	`chunk_id`
)
REFERENCES `chunk` (
	`id`
);

-- ── 비식별 관계 FK (ERDCloud 내보내기에서 빠지는 것들) ──────────
ALTER TABLE `department` ADD CONSTRAINT `FK_users_TO_department_1` FOREIGN KEY (`head_user_id`) REFERENCES `users` (`id`);
ALTER TABLE `users` ADD CONSTRAINT `FK_department_TO_users_1` FOREIGN KEY (`dept_id`) REFERENCES `department` (`id`);
ALTER TABLE `agent` ADD CONSTRAINT `FK_users_TO_agent_1` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`);
ALTER TABLE `agent_share_request` ADD CONSTRAINT `FK_agent_TO_agent_share_request_1` FOREIGN KEY (`agent_id`) REFERENCES `agent` (`id`);
ALTER TABLE `agent_share_request` ADD CONSTRAINT `FK_users_TO_agent_share_request_1` FOREIGN KEY (`requester_id`) REFERENCES `users` (`id`);
ALTER TABLE `agent_share_request` ADD CONSTRAINT `FK_users_TO_agent_share_request_2` FOREIGN KEY (`approver_id`) REFERENCES `users` (`id`);
ALTER TABLE `chat_session` ADD CONSTRAINT `FK_users_TO_chat_session_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`);
ALTER TABLE `chat_session` ADD CONSTRAINT `FK_agent_TO_chat_session_1` FOREIGN KEY (`agent_id`) REFERENCES `agent` (`id`);
ALTER TABLE `agent_run` ADD CONSTRAINT `FK_chat_session_TO_agent_run_1` FOREIGN KEY (`session_id`) REFERENCES `chat_session` (`id`);
ALTER TABLE `message` ADD CONSTRAINT `FK_chat_session_TO_message_1` FOREIGN KEY (`session_id`) REFERENCES `chat_session` (`id`);
ALTER TABLE `message` ADD CONSTRAINT `FK_agent_run_TO_message_1` FOREIGN KEY (`run_id`) REFERENCES `agent_run` (`id`);
ALTER TABLE `tool_call` ADD CONSTRAINT `FK_message_TO_tool_call_1` FOREIGN KEY (`message_id`) REFERENCES `message` (`id`);
ALTER TABLE `approval_request` ADD CONSTRAINT `FK_tool_call_TO_approval_request_1` FOREIGN KEY (`call_id`) REFERENCES `tool_call` (`call_id`);
ALTER TABLE `audit_log` ADD CONSTRAINT `FK_tool_call_TO_audit_log_1` FOREIGN KEY (`call_id`) REFERENCES `tool_call` (`call_id`);
ALTER TABLE `audit_log` ADD CONSTRAINT `FK_policy_rule_TO_audit_log_1` FOREIGN KEY (`rule_id`) REFERENCES `policy_rule` (`policy_rule_id`);
ALTER TABLE `document` ADD CONSTRAINT `FK_users_TO_document_1` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`);
ALTER TABLE `document` ADD CONSTRAINT `FK_department_TO_document_1` FOREIGN KEY (`owner_dept_id`) REFERENCES `department` (`id`);
ALTER TABLE `document_version` ADD CONSTRAINT `FK_document_TO_document_version_1` FOREIGN KEY (`document_id`) REFERENCES `document` (`id`);
ALTER TABLE `chunk` ADD CONSTRAINT `FK_document_version_TO_chunk_1` FOREIGN KEY (`document_version_id`) REFERENCES `document_version` (`id`);
