-- 업무 자동화 에이전트 ERD 최종본 (2026-11-18, ERDCloud 내보내기 기준)
-- 수정: CopyOfagent → agent_run 정리, message.id2/id3 → session_id/run_id,
--       ERDCloud 내보내기에서 빠진 비식별 관계 FK 를 파일 끝에 추가
-- 2026-11-20: v5 제안 일부 반영 (schema.postgres.sql 머리 주석 참고)
--   신규: agent_template, agent_template_tool, agent_assignment, agent_knowledge, work_file, output_file, output_source
--   삭제: session_doc (→ work_file)
--   변경: agent, message, tool_call, audit_log, doc, doc_version, chunk
-- 2026-11-23: 마이그레이션 0007~0010 반영 (자세한 내용은 schema.postgres.sql 머리 주석)
--   신규: login_session [0007]
--   변경: users [0007], agent·agent_share_request·chat_session [0008], agent_run [0009], audit_log [0007·0010]
--   삭제: audit_log → tool_call, audit_log → policy_rule 외래키 [0010]
CREATE TABLE `dept` (
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
	`security_grade`	INT	NOT NULL	DEFAULT 1	COMMENT '보안등급',
	`password_hash`	VARCHAR(255)	NULL	COMMENT '비밀번호 해시 (scrypt). 원문 저장 안 함. NULL 이면 비밀번호 로그인 불가 [0007]',
	`is_active`	BOOLEAN	NOT NULL	DEFAULT TRUE	COMMENT 'FALSE 면 로그인·기존 세션 모두 거부 [0007]',
	`failed_login_count`	INT	NOT NULL	DEFAULT 0	COMMENT '연속 로그인 실패 횟수 (성공하면 0) [0007]',
	`locked_until`	DATETIME	NULL	COMMENT '연속 실패로 이 시각까지 로그인 거부 [0007]',
	`last_login_at`	DATETIME	NULL	COMMENT '마지막 로그인 성공 시각 [0007]'
);

CREATE TABLE `login_session` (
	`token_hash`	CHAR(64)	NOT NULL	COMMENT '쿠키 토큰의 SHA-256 (토큰 원문은 저장 안 함) [0007]',
	`user_id`	BIGINT	NOT NULL	COMMENT '사용자 ID',
	`created_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '로그인 시각',
	`expires_at`	DATETIME	NOT NULL	COMMENT '만료 시각',
	`last_seen_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '마지막 사용 시각',
	`revoked_at`	DATETIME	NULL	COMMENT '로그아웃·강제 종료 시각',
	`client_ip`	VARCHAR(64)	NULL	COMMENT '접속 IP',
	`user_agent`	VARCHAR(300)	NULL	COMMENT '브라우저 정보'
);

CREATE TABLE `user_role` (
	`role`	VARCHAR(50)	NOT NULL	COMMENT '역할 (admin, dept_head, member)',
	`user_id`	BIGINT	NOT NULL	COMMENT '사용자 ID'
);

CREATE TABLE `agent_template` (
	`id`	BIGINT	NOT NULL	COMMENT '템플릿 ID',
	`name`	VARCHAR(100)	NOT NULL	COMMENT '템플릿 이름',
	`description`	TEXT	NULL	COMMENT '설명',
	`system_prompt`	TEXT	NOT NULL	COMMENT '시스템 프롬프트',
	`example`	TEXT	NULL	COMMENT '요청 예시',
	`created_by`	BIGINT	NOT NULL	COMMENT '만든 관리자 ID',
	`updated_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '수정 시각'
);

CREATE TABLE `agent_template_tool` (
	`tool_name`	VARCHAR(100)	NOT NULL	COMMENT '툴 이름',
	`template_id`	BIGINT	NOT NULL	COMMENT '템플릿 ID'
);

CREATE TABLE `agent` (
	`id`	BIGINT	NOT NULL	COMMENT '에이전트 ID',
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '만든 사용자 ID',
	`name`	VARCHAR(100)	NOT NULL	COMMENT '에이전트 이름',
	`description`	TEXT	NULL	COMMENT '설명',
	`system_prompt`	TEXT	NOT NULL	COMMENT '시스템 프롬프트',
	`scope`	VARCHAR(20)	NOT NULL	DEFAULT 'personal'	COMMENT '공개 범위 (personal, dept, company)',
	`status`	VARCHAR(20)	NOT NULL	DEFAULT 'active'	COMMENT '상태 (active, disabled)',
	`template_id`	BIGINT	NULL	COMMENT '만들 때 고른 템플릿 ID',
	`created_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '생성 시각',
	`updated_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '수정 시각',
	`disabled_by`	BIGINT	NULL	COMMENT '비활성화한 사람. 주인이 직접 끈 것만 주인이 다시 켤 수 있다 [0008]'
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
	`decided_at`	DATETIME	NULL	COMMENT '처리 시각',
	`decided_by`	BIGINT	NULL	COMMENT '승인·반려한 사용자 ID [0008]',
	`decision_comment`	TEXT	NULL	COMMENT '승인·반려 의견 [0008]'
);

CREATE TABLE `agent_assignment` (
	`agent_id`	BIGINT	NOT NULL	COMMENT '에이전트 ID',
	`dept_id`	BIGINT	NOT NULL	COMMENT '쓸 수 있는 부서 ID',
	`assigned_by`	BIGINT	NOT NULL	COMMENT '배정한 사용자 ID',
	`assigned_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '배정 시각'
);

CREATE TABLE `agent_knowledge` (
	`id`	BIGINT	NOT NULL	COMMENT '에이전트 지식 ID',
	`agent_id`	BIGINT	NOT NULL	COMMENT '에이전트 ID',
	`doc_id`	BIGINT	NULL	COMMENT '문서 ID (category 와 둘 중 하나)',
	`category`	VARCHAR(30)	NULL	COMMENT '문서 분류 (doc_id 와 둘 중 하나)'
);

CREATE TABLE `chat_session` (
	`id`	CHAR(36)	NOT NULL	COMMENT '세션 ID (UUID)',
	`user_id`	BIGINT	NOT NULL	COMMENT '사용자 ID',
	`agent_id`	BIGINT	NULL	COMMENT '사용한 에이전트 ID (NULL 이면 기본 에이전트)',
	`title`	VARCHAR(200)	NULL	COMMENT '세션 제목',
	`updated_at`	DATETIME	NULL	COMMENT '마지막 대화 시각',
	`agent_removed`	BOOLEAN	NOT NULL	DEFAULT FALSE	COMMENT '쓰던 에이전트가 삭제됨 (이 대화는 더 이어갈 수 없음) [0008]'
);

CREATE TABLE `message` (
	`id`	BIGINT	NOT NULL	COMMENT '메시지 ID',
	`seq`	INT	NOT NULL	COMMENT '세션 내 순서',
	`role`	VARCHAR(20)	NOT NULL	COMMENT '역할 (system, user, assistant, tool)',
	`content`	TEXT	NULL	COMMENT '내용',
	`tool_call_id`	VARCHAR(100)	NULL	COMMENT 'role=tool 일 때 대응 툴 호출 ID',
	`session_id`	CHAR(36)	NOT NULL	COMMENT '세션 ID',
	`run_id`	CHAR(36)	NULL	COMMENT '실행 ID (이 메시지를 만든 요청)',
	`has_work_data`	BOOLEAN	NOT NULL	DEFAULT FALSE	COMMENT '작업 파일 내용 포함 여부 (만료 때 content 삭제)'
);

CREATE TABLE `tool_call` (
	`call_id`	VARCHAR(100)	NOT NULL	COMMENT '툴 호출 ID (LLM 이 준 id)',
	`message_id`	BIGINT	NOT NULL	COMMENT '요청한 assistant 메시지 ID',
	`seq`	INT	NOT NULL	COMMENT '응답 내 호출 순서',
	`tool_name`	VARCHAR(100)	NOT NULL	COMMENT '툴 이름',
	`arguments`	JSON	NULL	COMMENT '툴 인자',
	`ok`	BOOLEAN	NULL	COMMENT '성공 여부',
	`result_content`	TEXT	NULL	COMMENT 'LLM 에게 돌려준 결과',
	`duration_ms`	INT	NULL	COMMENT '실행 시간(ms)',
	`has_work_data`	BOOLEAN	NOT NULL	DEFAULT FALSE	COMMENT '결과에 작업 파일 내용 포함 여부 (만료 때 result_content 삭제)'
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
	`event_type`	VARCHAR(30)	NOT NULL	DEFAULT 'tool_call'	COMMENT '기록 종류 (tool_call, doc_register, work_upload, work_purge, output_download, agent_share, login, login_failed, logout [0007])',
	`call_id`	VARCHAR(100)	NULL	COMMENT '툴 호출 ID (tool_call 일 때 필수). 외래키 없음 — 실행 도중 기록, 대화를 지워도 남게 [0010]',
	`session_id`	CHAR(36)	NULL	COMMENT '세션 ID (tool_call 일 때 필수)',
	`user_id`	BIGINT	NOT NULL	COMMENT '요청자 ID',
	`agent_id`	BIGINT	NULL	COMMENT '호출한 에이전트 ID (기록용, FK 없음)',
	`tool_name`	VARCHAR(100)	NULL	COMMENT '툴 이름 (tool_call 일 때 필수)',
	`arguments`	JSON	NULL	COMMENT '툴 인자',
	`principal_snapshot`	JSON	NULL	COMMENT '판정 시점 요청자 속성',
	`decision`	VARCHAR(10)	NULL	COMMENT '판정 (allow, deny, ask) (tool_call 일 때 필수)',
	`reason`	TEXT	NULL	COMMENT '판정 사유',
	`rule_id`	VARCHAR(100)	NULL	COMMENT '매칭된 규칙 ID (없으면 기본값). config/permissions.yaml 의 id, 외래키 없음 [0010]',
	`approval`	VARCHAR(20)	NULL	COMMENT 'ASK 일 때 승인 결과 (approved, rejected, timeout)',
	`executed`	BOOLEAN	NOT NULL	DEFAULT FALSE	COMMENT '실행 여부',
	`tool_ok`	BOOLEAN	NULL	COMMENT '실행했을 때 툴 성공 여부 (실행 안 했으면 NULL)',
	`target_type`	VARCHAR(30)	NULL	COMMENT '대상 종류 (doc, work_file, output_file, agent)',
	`target_id`	BIGINT	NULL	COMMENT '대상 ID (기록용, FK 없음)',
	`ts`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '판정 시각 (UTC)',
	`run_id`	CHAR(36)	NULL	COMMENT '판정이 나온 요청 (agent_run.id, 외래키 없음) [0010]'
);

CREATE TABLE `doc` (
	`id`	BIGINT	NOT NULL	COMMENT '문서 ID',
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '등록한 사용자 ID',
	`owner_dept_id`	BIGINT	NOT NULL	COMMENT '관리 책임 부서 ID',
	`title`	VARCHAR(255)	NOT NULL	COMMENT '문서명',
	`category`	VARCHAR(30)	NOT NULL	COMMENT '분류 (내규, 매뉴얼, 상품, 법령, 공문)',
	`scope`	VARCHAR(20)	NOT NULL	DEFAULT 'company'	COMMENT '공개 범위 (dept, company)',
	`security_grade`	INT	NOT NULL	DEFAULT 1	COMMENT '보안등급',
	`status`	VARCHAR(20)	NOT NULL	DEFAULT 'active'	COMMENT '상태 (active, retired)'
);

CREATE TABLE `doc_version` (
	`id`	BIGINT	NOT NULL	COMMENT '문서 버전 ID',
	`doc_id`	BIGINT	NOT NULL	COMMENT '문서 ID',
	`version`	INT	NOT NULL	COMMENT '버전 번호',
	`is_current`	BOOLEAN	NOT NULL	DEFAULT TRUE	COMMENT '현재 버전 여부',
	`file_name`	VARCHAR(255)	NOT NULL	COMMENT '원본 파일명',
	`mime_type`	VARCHAR(100)	NOT NULL	COMMENT '파일 형식',
	`storage_path`	VARCHAR(500)	NOT NULL	COMMENT '저장 경로',
	`status`	VARCHAR(20)	NOT NULL	DEFAULT 'processing'	COMMENT '처리 상태 (processing, ready, failed)',
	`sha256`	CHAR(64)	NOT NULL	COMMENT '파일 해시 (중복 등록 막기)',
	`effective_date`	DATE	NULL	COMMENT '시행일',
	`parse_profile`	VARCHAR(20)	NOT NULL	DEFAULT 'accurate'	COMMENT '파싱 설정',
	`parsed_path`	VARCHAR(500)	NULL	COMMENT '파싱 결과 JSON 경로',
	`registered_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '등록 시각'
);

CREATE TABLE `chunk` (
	`id`	BIGINT	NOT NULL	COMMENT '청크 ID',
	`doc_version_id`	BIGINT	NOT NULL	COMMENT '문서 버전 ID',
	`chunk_index`	INT	NOT NULL	COMMENT '청크 순번',
	`page`	INT	NULL	COMMENT '페이지',
	`content`	TEXT	NOT NULL	COMMENT '청크 본문',
	`owner_dept_id`	BIGINT	NOT NULL	COMMENT '소유 부서 ID (문서에서 상속)',
	`scope`	VARCHAR(20)	NOT NULL	COMMENT '공개 범위 (문서에서 상속)',
	`security_grade`	INT	NOT NULL	COMMENT '보안등급 (문서에서 상속)',
	`heading_path`	JSON	NULL	COMMENT '제목 경로 (예: ["제3장", "제42조"])',
	`vector_id`	VARCHAR(100)	NULL	COMMENT '벡터 DB 키 (Postgres 에서는 embedding 컬럼)'
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
	`ended_at`	DATETIME	NULL	COMMENT '종료 시각 (실행 중이면 NULL)',
	`denial`	JSON	NULL	COMMENT '권한 거부로 멈춘 요청의 거부 내용 {call_id, tool_name, display_name, kind, reason} [0009]'
);

CREATE TABLE `work_file` (
	`id`	BIGINT	NOT NULL	COMMENT '작업 파일 ID',
	`session_id`	CHAR(36)	NULL	COMMENT '올린 대화 ID (대화가 지워지면 NULL)',
	`user_id`	BIGINT	NOT NULL	COMMENT '올린 사용자 ID',
	`file_name`	VARCHAR(255)	NOT NULL	COMMENT '파일명',
	`mime_type`	VARCHAR(100)	NOT NULL	COMMENT '파일 형식',
	`size_bytes`	BIGINT	NOT NULL	COMMENT '크기(byte)',
	`sha256`	CHAR(64)	NOT NULL	COMMENT '파일 해시 (삭제 후에도 남김)',
	`storage_path`	VARCHAR(500)	NULL	COMMENT '원본 경로 (파싱 확인 후 NULL)',
	`original_deleted_at`	DATETIME	NULL	COMMENT '원본 삭제 시각',
	`parsed_path`	VARCHAR(500)	NULL	COMMENT '파싱 결과 JSON 경로 (만료 시 NULL)',
	`pii_found`	BOOLEAN	NOT NULL	DEFAULT FALSE	COMMENT '개인정보 발견 여부',
	`pii_summary`	JSON	NULL	COMMENT '개인정보 종류별 건수',
	`status`	VARCHAR(20)	NOT NULL	DEFAULT 'uploaded'	COMMENT '상태 (uploaded, parsing, ready, failed, expired)',
	`uploaded_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '업로드 시각',
	`last_used_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '마지막 사용 시각',
	`expires_at`	DATETIME	NOT NULL	COMMENT '파싱본 만료 시각',
	`deleted_at`	DATETIME	NULL	COMMENT '파싱본까지 지운 시각'
);

CREATE TABLE `output_file` (
	`id`	BIGINT	NOT NULL	COMMENT '산출물 ID',
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '만든 사용자 ID',
	`session_id`	CHAR(36)	NULL	COMMENT '만든 대화 ID',
	`run_id`	CHAR(36)	NULL	COMMENT '만든 실행 ID',
	`title`	VARCHAR(200)	NOT NULL	COMMENT '제목',
	`file_name`	VARCHAR(255)	NOT NULL	COMMENT '파일명',
	`mime_type`	VARCHAR(100)	NOT NULL	COMMENT '파일 형식',
	`storage_path`	VARCHAR(500)	NOT NULL	COMMENT '저장 경로',
	`sha256`	CHAR(64)	NOT NULL	COMMENT '파일 해시',
	`has_customer_info`	BOOLEAN	NOT NULL	DEFAULT FALSE	COMMENT '고객정보 포함 여부',
	`created_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '생성 시각',
	`deleted_at`	DATETIME	NULL	COMMENT '삭제 시각'
);

CREATE TABLE `output_source` (
	`id`	BIGINT	NOT NULL	COMMENT '산출물 입력 ID',
	`output_id`	BIGINT	NOT NULL	COMMENT '산출물 ID',
	`work_file_id`	BIGINT	NULL	COMMENT '작업 파일 ID (doc_version_id 와 둘 중 하나)',
	`doc_version_id`	BIGINT	NULL	COMMENT '문서 버전 ID (work_file_id 와 둘 중 하나)'
);

ALTER TABLE `dept` ADD CONSTRAINT `PK_DEPT` PRIMARY KEY (
	`id`
);

ALTER TABLE `users` ADD CONSTRAINT `PK_USERS` PRIMARY KEY (
	`id`
);

ALTER TABLE `login_session` ADD CONSTRAINT `PK_LOGIN_SESSION` PRIMARY KEY (
	`token_hash`
);

ALTER TABLE `user_role` ADD CONSTRAINT `PK_USER_ROLE` PRIMARY KEY (
	`role`,
	`user_id`
);

ALTER TABLE `agent_template` ADD CONSTRAINT `PK_AGENT_TEMPLATE` PRIMARY KEY (
	`id`
);

ALTER TABLE `agent_template_tool` ADD CONSTRAINT `PK_AGENT_TEMPLATE_TOOL` PRIMARY KEY (
	`tool_name`,
	`template_id`
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

ALTER TABLE `agent_assignment` ADD CONSTRAINT `PK_AGENT_ASSIGNMENT` PRIMARY KEY (
	`agent_id`,
	`dept_id`
);

ALTER TABLE `agent_knowledge` ADD CONSTRAINT `PK_AGENT_KNOWLEDGE` PRIMARY KEY (
	`id`
);

ALTER TABLE `chat_session` ADD CONSTRAINT `PK_CHAT_SESSION` PRIMARY KEY (
	`id`
);

ALTER TABLE `message` ADD CONSTRAINT `PK_MESSAGE` PRIMARY KEY (
	`id`
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

ALTER TABLE `doc` ADD CONSTRAINT `PK_DOC` PRIMARY KEY (
	`id`
);

ALTER TABLE `doc_version` ADD CONSTRAINT `PK_DOC_VERSION` PRIMARY KEY (
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

ALTER TABLE `work_file` ADD CONSTRAINT `PK_WORK_FILE` PRIMARY KEY (
	`id`
);

ALTER TABLE `output_file` ADD CONSTRAINT `PK_OUTPUT_FILE` PRIMARY KEY (
	`id`
);

ALTER TABLE `output_source` ADD CONSTRAINT `PK_OUTPUT_SOURCE` PRIMARY KEY (
	`id`
);

ALTER TABLE `user_role` ADD CONSTRAINT `FK_users_TO_user_role_1` FOREIGN KEY (
	`user_id`
)
REFERENCES `users` (
	`id`
);

ALTER TABLE `agent_template_tool` ADD CONSTRAINT `FK_agent_template_TO_agent_template_tool_1` FOREIGN KEY (
	`template_id`
)
REFERENCES `agent_template` (
	`id`
);

ALTER TABLE `agent_tool` ADD CONSTRAINT `FK_agent_TO_agent_tool_1` FOREIGN KEY (
	`agent_id`
)
REFERENCES `agent` (
	`id`
);

ALTER TABLE `agent_assignment` ADD CONSTRAINT `FK_agent_TO_agent_assignment_1` FOREIGN KEY (
	`agent_id`
)
REFERENCES `agent` (
	`id`
);

ALTER TABLE `agent_assignment` ADD CONSTRAINT `FK_dept_TO_agent_assignment_1` FOREIGN KEY (
	`dept_id`
)
REFERENCES `dept` (
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
ALTER TABLE `dept` ADD CONSTRAINT `FK_users_TO_dept_1` FOREIGN KEY (`head_user_id`) REFERENCES `users` (`id`);
ALTER TABLE `login_session` ADD CONSTRAINT `FK_users_TO_login_session_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`);
ALTER TABLE `agent` ADD CONSTRAINT `FK_users_TO_agent_2` FOREIGN KEY (`disabled_by`) REFERENCES `users` (`id`);
ALTER TABLE `agent_share_request` ADD CONSTRAINT `FK_users_TO_agent_share_request_3` FOREIGN KEY (`decided_by`) REFERENCES `users` (`id`);
ALTER TABLE `users` ADD CONSTRAINT `FK_dept_TO_users_1` FOREIGN KEY (`dept_id`) REFERENCES `dept` (`id`);
ALTER TABLE `agent_template` ADD CONSTRAINT `FK_users_TO_agent_template_1` FOREIGN KEY (`created_by`) REFERENCES `users` (`id`);
ALTER TABLE `agent` ADD CONSTRAINT `FK_users_TO_agent_1` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`);
ALTER TABLE `agent` ADD CONSTRAINT `FK_agent_template_TO_agent_1` FOREIGN KEY (`template_id`) REFERENCES `agent_template` (`id`);
ALTER TABLE `agent_share_request` ADD CONSTRAINT `FK_agent_TO_agent_share_request_1` FOREIGN KEY (`agent_id`) REFERENCES `agent` (`id`);
ALTER TABLE `agent_share_request` ADD CONSTRAINT `FK_users_TO_agent_share_request_1` FOREIGN KEY (`requester_id`) REFERENCES `users` (`id`);
ALTER TABLE `agent_share_request` ADD CONSTRAINT `FK_users_TO_agent_share_request_2` FOREIGN KEY (`approver_id`) REFERENCES `users` (`id`);
ALTER TABLE `agent_assignment` ADD CONSTRAINT `FK_users_TO_agent_assignment_1` FOREIGN KEY (`assigned_by`) REFERENCES `users` (`id`);
ALTER TABLE `agent_knowledge` ADD CONSTRAINT `FK_agent_TO_agent_knowledge_1` FOREIGN KEY (`agent_id`) REFERENCES `agent` (`id`);
ALTER TABLE `agent_knowledge` ADD CONSTRAINT `FK_doc_TO_agent_knowledge_1` FOREIGN KEY (`doc_id`) REFERENCES `doc` (`id`);
ALTER TABLE `chat_session` ADD CONSTRAINT `FK_users_TO_chat_session_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`);
ALTER TABLE `chat_session` ADD CONSTRAINT `FK_agent_TO_chat_session_1` FOREIGN KEY (`agent_id`) REFERENCES `agent` (`id`);
ALTER TABLE `agent_run` ADD CONSTRAINT `FK_chat_session_TO_agent_run_1` FOREIGN KEY (`session_id`) REFERENCES `chat_session` (`id`);
ALTER TABLE `message` ADD CONSTRAINT `FK_chat_session_TO_message_1` FOREIGN KEY (`session_id`) REFERENCES `chat_session` (`id`);
ALTER TABLE `message` ADD CONSTRAINT `FK_agent_run_TO_message_1` FOREIGN KEY (`run_id`) REFERENCES `agent_run` (`id`);
ALTER TABLE `tool_call` ADD CONSTRAINT `FK_message_TO_tool_call_1` FOREIGN KEY (`message_id`) REFERENCES `message` (`id`);
ALTER TABLE `approval_request` ADD CONSTRAINT `FK_tool_call_TO_approval_request_1` FOREIGN KEY (`call_id`) REFERENCES `tool_call` (`call_id`);
-- [0010] audit_log → tool_call, audit_log → policy_rule 외래키는 뺐다 (감사 기록은 실행 도중 쓰이고 대화를 지워도 남아야 한다)
ALTER TABLE `doc` ADD CONSTRAINT `FK_users_TO_doc_1` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`);
ALTER TABLE `doc` ADD CONSTRAINT `FK_dept_TO_doc_1` FOREIGN KEY (`owner_dept_id`) REFERENCES `dept` (`id`);
ALTER TABLE `doc_version` ADD CONSTRAINT `FK_doc_TO_doc_version_1` FOREIGN KEY (`doc_id`) REFERENCES `doc` (`id`);
ALTER TABLE `chunk` ADD CONSTRAINT `FK_doc_version_TO_chunk_1` FOREIGN KEY (`doc_version_id`) REFERENCES `doc_version` (`id`);
ALTER TABLE `work_file` ADD CONSTRAINT `FK_chat_session_TO_work_file_1` FOREIGN KEY (`session_id`) REFERENCES `chat_session` (`id`);
ALTER TABLE `work_file` ADD CONSTRAINT `FK_users_TO_work_file_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`);
ALTER TABLE `output_file` ADD CONSTRAINT `FK_users_TO_output_file_1` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`);
ALTER TABLE `output_file` ADD CONSTRAINT `FK_chat_session_TO_output_file_1` FOREIGN KEY (`session_id`) REFERENCES `chat_session` (`id`);
ALTER TABLE `output_file` ADD CONSTRAINT `FK_agent_run_TO_output_file_1` FOREIGN KEY (`run_id`) REFERENCES `agent_run` (`id`);
ALTER TABLE `output_source` ADD CONSTRAINT `FK_output_file_TO_output_source_1` FOREIGN KEY (`output_id`) REFERENCES `output_file` (`id`);
ALTER TABLE `output_source` ADD CONSTRAINT `FK_work_file_TO_output_source_1` FOREIGN KEY (`work_file_id`) REFERENCES `work_file` (`id`);
ALTER TABLE `output_source` ADD CONSTRAINT `FK_doc_version_TO_output_source_1` FOREIGN KEY (`doc_version_id`) REFERENCES `doc_version` (`id`);
