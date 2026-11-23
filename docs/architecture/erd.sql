-- 업무 자동화 에이전트 ERD 최종본 (2026-11-18, ERDCloud 내보내기 기준)
-- 수정: CopyOfagent → agent_run 정리, message.id2/id3 → session_id/run_id,
--       ERDCloud 내보내기에서 빠진 비식별 관계 FK 를 파일 끝에 추가
-- 2026-11-20: v5 제안 일부 반영 (schema.postgres.sql 머리 주석 참고)
--   신규: agent_template, agent_template_tool, agent_assignment, agent_knowledge, work_file, output_file, output_source
--   삭제: session_document (→ work_file)
--   변경: agent, message, tool_call, audit_log, document, document_version, chunk
-- 2026-11-23 [alembic 0006 초안]: mail_archive 추가 — 직원(users)을 지우기 직전 트리거가 그 직원의 mail_message 를 복사해 둔다 (퇴사자 메일을 관리자가 관리). users 외래키 없음.
-- 2026-11-23: 개인 메일 표 5개 추가 [alembic 0005 초안] — mail_account, mail_message, mail_sync_state, mail_sync_failure, mail_sync_run
--   직원(users) 삭제 시 메일도 삭제 (ON DELETE CASCADE). 실제 DB 는 PostgreSQL 이라 JSON 은 JSONB, DATETIME 은 TIMESTAMPTZ 로 만든다.
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
	`updated_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '수정 시각'
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

CREATE TABLE `agent_assignment` (
	`agent_id`	BIGINT	NOT NULL	COMMENT '에이전트 ID',
	`dept_id`	BIGINT	NOT NULL	COMMENT '쓸 수 있는 부서 ID',
	`assigned_by`	BIGINT	NOT NULL	COMMENT '배정한 사용자 ID',
	`assigned_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '배정 시각'
);

CREATE TABLE `agent_knowledge` (
	`id`	BIGINT	NOT NULL	COMMENT '에이전트 지식 ID',
	`agent_id`	BIGINT	NOT NULL	COMMENT '에이전트 ID',
	`document_id`	BIGINT	NULL	COMMENT '문서 ID (category 와 둘 중 하나)',
	`category`	VARCHAR(30)	NULL	COMMENT '문서 분류 (document_id 와 둘 중 하나)'
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
	`event_type`	VARCHAR(30)	NOT NULL	DEFAULT 'tool_call'	COMMENT '기록 종류 (tool_call, doc_register, work_upload, work_purge, output_download, agent_share)',
	`call_id`	VARCHAR(100)	NULL	COMMENT '툴 호출 ID (tool_call 일 때 필수)',
	`session_id`	CHAR(36)	NULL	COMMENT '세션 ID (tool_call 일 때 필수)',
	`user_id`	BIGINT	NOT NULL	COMMENT '요청자 ID',
	`agent_id`	BIGINT	NULL	COMMENT '호출한 에이전트 ID (기록용, FK 없음)',
	`tool_name`	VARCHAR(100)	NULL	COMMENT '툴 이름 (tool_call 일 때 필수)',
	`arguments`	JSON	NULL	COMMENT '툴 인자',
	`principal_snapshot`	JSON	NULL	COMMENT '판정 시점 요청자 속성',
	`decision`	VARCHAR(10)	NULL	COMMENT '판정 (allow, deny, ask) (tool_call 일 때 필수)',
	`reason`	TEXT	NULL	COMMENT '판정 사유',
	`rule_id`	VARCHAR(100)	NULL	COMMENT '매칭된 규칙 ID (없으면 기본값)',
	`approval`	VARCHAR(20)	NULL	COMMENT 'ASK 일 때 승인 결과 (approved, rejected, timeout)',
	`executed`	BOOLEAN	NOT NULL	DEFAULT FALSE	COMMENT '실행 여부',
	`tool_ok`	BOOLEAN	NULL	COMMENT '실행했을 때 툴 성공 여부 (실행 안 했으면 NULL)',
	`target_type`	VARCHAR(30)	NULL	COMMENT '대상 종류 (document, work_file, output_file, agent)',
	`target_id`	BIGINT	NULL	COMMENT '대상 ID (기록용, FK 없음)',
	`ts`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '판정 시각 (UTC)'
);

CREATE TABLE `document` (
	`id`	BIGINT	NOT NULL	COMMENT '문서 ID',
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '등록한 사용자 ID',
	`owner_dept_id`	BIGINT	NOT NULL	COMMENT '관리 책임 부서 ID',
	`title`	VARCHAR(255)	NOT NULL	COMMENT '문서명',
	`category`	VARCHAR(30)	NOT NULL	COMMENT '분류 (내규, 매뉴얼, 상품, 법령, 공문)',
	`scope`	VARCHAR(20)	NOT NULL	DEFAULT 'company'	COMMENT '공개 범위 (dept, company)',
	`security_grade`	INT	NOT NULL	DEFAULT 1	COMMENT '보안등급',
	`status`	VARCHAR(20)	NOT NULL	DEFAULT 'active'	COMMENT '상태 (active, retired)'
);

CREATE TABLE `document_version` (
	`id`	BIGINT	NOT NULL	COMMENT '문서 버전 ID',
	`document_id`	BIGINT	NOT NULL	COMMENT '문서 ID',
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
	`document_version_id`	BIGINT	NOT NULL	COMMENT '문서 버전 ID',
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
	`ended_at`	DATETIME	NULL	COMMENT '종료 시각 (실행 중이면 NULL)'
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
	`work_file_id`	BIGINT	NULL	COMMENT '작업 파일 ID (document_version_id 와 둘 중 하나)',
	`document_version_id`	BIGINT	NULL	COMMENT '문서 버전 ID (work_file_id 와 둘 중 하나)'
);

CREATE TABLE `mail_account` (
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '직원 ID (users.id)',
	`email`	VARCHAR(255)	NOT NULL	COMMENT '연결한 메일 주소',
	`connected_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '연결 시각'
);

CREATE TABLE `mail_message` (
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '메일함 주인 (users.id) — 이 사람만 조회 가능',
	`message_id`	VARCHAR(64)	NOT NULL	COMMENT 'Gmail 메시지 ID',
	`thread_id`	VARCHAR(64)	NOT NULL	COMMENT 'Gmail 스레드 ID',
	`sender`	TEXT	NOT NULL	DEFAULT ''	COMMENT '보낸 사람',
	`to_addrs`	JSON	NOT NULL	COMMENT '받는 사람 목록',
	`cc_addrs`	JSON	NOT NULL	COMMENT '참조 목록',
	`bcc_addrs`	JSON	NOT NULL	COMMENT '숨은참조 목록 (본인이 보낸 메일에만 있음)',
	`subject`	TEXT	NOT NULL	DEFAULT ''	COMMENT '제목',
	`sent_at`	DATETIME	NULL	COMMENT '보낸 시각',
	`labels`	JSON	NOT NULL	COMMENT 'Gmail 라벨',
	`body`	TEXT	NOT NULL	DEFAULT ''	COMMENT '본문 텍스트',
	`body_omitted`	BOOLEAN	NOT NULL	DEFAULT FALSE	COMMENT '본문 일부가 너무 커서 생략됨',
	`size_bytes`	INT	NOT NULL	DEFAULT 0	COMMENT '원본 크기',
	`content_hash`	VARCHAR(80)	NOT NULL	COMMENT '저장 내용 해시 — 같으면 다시 쓰지 않음',
	`synced_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '마지막 저장 시각'
);

CREATE TABLE `mail_sync_state` (
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '직원 ID',
	`last_sync_end`	DOUBLE	NULL	COMMENT '마지막으로 끝까지 성공한 동기화 창의 끝 (epoch 초)',
	`window_start`	DOUBLE	NULL	COMMENT '진행 중인 창 시작 (epoch 초)',
	`window_end`	DOUBLE	NULL	COMMENT '진행 중인 창 끝 (epoch 초)',
	`checkpoint`	JSON	NULL	COMMENT '진행 중 체크포인트 (다음 페이지 토큰) — NULL 이면 진행 중 아님',
	`job_id`	VARCHAR(64)	NULL	COMMENT '진행 중 작업 ID'
);

CREATE TABLE `mail_sync_failure` (
	`id`	BIGINT	NOT NULL	COMMENT '실패 기록 ID',
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '직원 ID',
	`job_id`	VARCHAR(64)	NOT NULL	COMMENT '동기화 작업 ID',
	`message_id`	VARCHAR(64)	NULL	COMMENT 'Gmail 메시지 ID',
	`reason`	VARCHAR(30)	NOT NULL	COMMENT '실패 사유 (fetch_error, too_large, invalid_message)',
	`detail`	TEXT	NOT NULL	DEFAULT ''	COMMENT '상세',
	`created_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '기록 시각'
);

CREATE TABLE `mail_sync_run` (
	`id`	BIGINT	NOT NULL	COMMENT '실행 기록 ID',
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '직원 ID',
	`job_id`	VARCHAR(64)	NOT NULL	COMMENT '동기화 작업 ID',
	`mode`	VARCHAR(20)	NOT NULL	COMMENT '방식 (incremental, full, prune)',
	`summary`	JSON	NOT NULL	COMMENT '건수 · 사유별 실패 수',
	`finished_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '끝난 시각'
);

CREATE TABLE `mail_archive` (
	`former_user_id`	BIGINT	NOT NULL	COMMENT '삭제된 직원의 users.id (FK 없음 — 직원이 이미 지워짐)',
	`message_id`	VARCHAR(64)	NOT NULL	COMMENT 'Gmail 메시지 ID',
	`former_login_id`	VARCHAR(100)	NOT NULL	COMMENT '삭제 당시 로그인 ID',
	`former_name`	VARCHAR(100)	NOT NULL	COMMENT '삭제 당시 이름',
	`former_dept_id`	BIGINT	NULL	COMMENT '삭제 당시 소속 부서 ID (FK 없음)',
	`account_email`	VARCHAR(255)	NULL	COMMENT '연결해 두었던 메일 주소',
	`thread_id`	VARCHAR(64)	NOT NULL	COMMENT 'Gmail 스레드 ID',
	`sender`	TEXT	NOT NULL	DEFAULT ''	COMMENT '보낸 사람',
	`to_addrs`	JSON	NOT NULL	COMMENT '받는 사람 목록',
	`cc_addrs`	JSON	NOT NULL	COMMENT '참조 목록',
	`bcc_addrs`	JSON	NOT NULL	COMMENT '숨은참조 목록',
	`subject`	TEXT	NOT NULL	DEFAULT ''	COMMENT '제목',
	`sent_at`	DATETIME	NULL	COMMENT '보낸 시각',
	`labels`	JSON	NOT NULL	COMMENT 'Gmail 라벨',
	`body`	TEXT	NOT NULL	DEFAULT ''	COMMENT '본문 텍스트',
	`body_omitted`	BOOLEAN	NOT NULL	DEFAULT FALSE	COMMENT '본문 일부가 너무 커서 생략됨',
	`size_bytes`	INT	NOT NULL	DEFAULT 0	COMMENT '원본 크기',
	`content_hash`	VARCHAR(80)	NOT NULL	COMMENT '저장 내용 해시',
	`synced_at`	DATETIME	NOT NULL	COMMENT '마지막 저장 시각',
	`archived_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '보관 시각 (직원이 삭제된 시각)'
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

ALTER TABLE `work_file` ADD CONSTRAINT `PK_WORK_FILE` PRIMARY KEY (
	`id`
);

ALTER TABLE `output_file` ADD CONSTRAINT `PK_OUTPUT_FILE` PRIMARY KEY (
	`id`
);

ALTER TABLE `output_source` ADD CONSTRAINT `PK_OUTPUT_SOURCE` PRIMARY KEY (
	`id`
);

ALTER TABLE `mail_account` ADD CONSTRAINT `PK_MAIL_ACCOUNT` PRIMARY KEY (
	`owner_user_id`
);

ALTER TABLE `mail_message` ADD CONSTRAINT `PK_MAIL_MESSAGE` PRIMARY KEY (
	`owner_user_id`,
	`message_id`
);

ALTER TABLE `mail_sync_state` ADD CONSTRAINT `PK_MAIL_SYNC_STATE` PRIMARY KEY (
	`owner_user_id`
);

ALTER TABLE `mail_sync_failure` ADD CONSTRAINT `PK_MAIL_SYNC_FAILURE` PRIMARY KEY (
	`id`
);

ALTER TABLE `mail_sync_run` ADD CONSTRAINT `PK_MAIL_SYNC_RUN` PRIMARY KEY (
	`id`
);

ALTER TABLE `mail_archive` ADD CONSTRAINT `PK_MAIL_ARCHIVE` PRIMARY KEY (
	`former_user_id`,
	`message_id`
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

ALTER TABLE `agent_assignment` ADD CONSTRAINT `FK_department_TO_agent_assignment_1` FOREIGN KEY (
	`dept_id`
)
REFERENCES `department` (
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
ALTER TABLE `agent_template` ADD CONSTRAINT `FK_users_TO_agent_template_1` FOREIGN KEY (`created_by`) REFERENCES `users` (`id`);
ALTER TABLE `agent` ADD CONSTRAINT `FK_users_TO_agent_1` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`);
ALTER TABLE `agent` ADD CONSTRAINT `FK_agent_template_TO_agent_1` FOREIGN KEY (`template_id`) REFERENCES `agent_template` (`id`);
ALTER TABLE `agent_share_request` ADD CONSTRAINT `FK_agent_TO_agent_share_request_1` FOREIGN KEY (`agent_id`) REFERENCES `agent` (`id`);
ALTER TABLE `agent_share_request` ADD CONSTRAINT `FK_users_TO_agent_share_request_1` FOREIGN KEY (`requester_id`) REFERENCES `users` (`id`);
ALTER TABLE `agent_share_request` ADD CONSTRAINT `FK_users_TO_agent_share_request_2` FOREIGN KEY (`approver_id`) REFERENCES `users` (`id`);
ALTER TABLE `agent_assignment` ADD CONSTRAINT `FK_users_TO_agent_assignment_1` FOREIGN KEY (`assigned_by`) REFERENCES `users` (`id`);
ALTER TABLE `agent_knowledge` ADD CONSTRAINT `FK_agent_TO_agent_knowledge_1` FOREIGN KEY (`agent_id`) REFERENCES `agent` (`id`);
ALTER TABLE `agent_knowledge` ADD CONSTRAINT `FK_document_TO_agent_knowledge_1` FOREIGN KEY (`document_id`) REFERENCES `document` (`id`);
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
ALTER TABLE `work_file` ADD CONSTRAINT `FK_chat_session_TO_work_file_1` FOREIGN KEY (`session_id`) REFERENCES `chat_session` (`id`);
ALTER TABLE `work_file` ADD CONSTRAINT `FK_users_TO_work_file_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`);
ALTER TABLE `output_file` ADD CONSTRAINT `FK_users_TO_output_file_1` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`);
ALTER TABLE `output_file` ADD CONSTRAINT `FK_chat_session_TO_output_file_1` FOREIGN KEY (`session_id`) REFERENCES `chat_session` (`id`);
ALTER TABLE `output_file` ADD CONSTRAINT `FK_agent_run_TO_output_file_1` FOREIGN KEY (`run_id`) REFERENCES `agent_run` (`id`);
ALTER TABLE `output_source` ADD CONSTRAINT `FK_output_file_TO_output_source_1` FOREIGN KEY (`output_id`) REFERENCES `output_file` (`id`);
ALTER TABLE `output_source` ADD CONSTRAINT `FK_work_file_TO_output_source_1` FOREIGN KEY (`work_file_id`) REFERENCES `work_file` (`id`);
ALTER TABLE `output_source` ADD CONSTRAINT `FK_document_version_TO_output_source_1` FOREIGN KEY (`document_version_id`) REFERENCES `document_version` (`id`);
ALTER TABLE `mail_account` ADD CONSTRAINT `FK_users_TO_mail_account_1` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE;
ALTER TABLE `mail_message` ADD CONSTRAINT `FK_users_TO_mail_message_1` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE;
ALTER TABLE `mail_sync_state` ADD CONSTRAINT `FK_users_TO_mail_sync_state_1` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE;
ALTER TABLE `mail_sync_failure` ADD CONSTRAINT `FK_users_TO_mail_sync_failure_1` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE;
ALTER TABLE `mail_sync_run` ADD CONSTRAINT `FK_users_TO_mail_sync_run_1` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE;
