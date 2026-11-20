-- 개인 메일 저장 테이블 (ERD 추가 제안, ERD final 과 같은 MySQL 문법)
-- 개발 중에는 같은 구조를 SQLite(mcp-servers/gmail/src/gmail_mcp/store.py)로 사용한다.
--
-- 원칙
--   · 직원 개인 메일은 벡터DB · chunk 에 넣지 않고 이 테이블에만 저장한다.
--   · 모든 조회는 owner_user_id = 로그인 사용자 조건을 붙인다 (커넥터 코드가 강제).
--   · 기존 document · document_version · chunk 는 건드리지 않는다.

CREATE TABLE `mail_account` (
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '직원 ID (users.id)',
	`email`	VARCHAR(255)	NOT NULL	COMMENT '연결한 메일 주소',
	`connected_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '연결 시각',
	PRIMARY KEY (`owner_user_id`)
);

CREATE TABLE `mail_message` (
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '메일함 주인 (users.id) — 이 사람만 조회 가능',
	`message_id`	VARCHAR(64)	NOT NULL	COMMENT 'Gmail 메시지 ID',
	`thread_id`	VARCHAR(64)	NOT NULL	COMMENT 'Gmail 스레드 ID',
	`sender`	VARCHAR(255)	NOT NULL	DEFAULT ''	COMMENT '보낸 사람',
	`to_addrs`	JSON	NOT NULL	COMMENT '받는 사람 목록',
	`cc_addrs`	JSON	NOT NULL	COMMENT '참조 목록',
	`bcc_addrs`	JSON	NOT NULL	COMMENT '숨은참조 목록 (본인이 보낸 메일에만 있음)',
	`subject`	VARCHAR(500)	NOT NULL	DEFAULT ''	COMMENT '제목',
	`sent_at`	DATETIME	NULL	COMMENT '보낸 시각',
	`labels`	JSON	NOT NULL	COMMENT 'Gmail 라벨',
	`body`	MEDIUMTEXT	NOT NULL	COMMENT '본문 텍스트',
	`body_omitted`	BOOLEAN	NOT NULL	DEFAULT FALSE	COMMENT '본문 일부가 너무 커서 생략됨',
	`size_bytes`	INT	NOT NULL	DEFAULT 0	COMMENT '원본 크기',
	`content_hash`	VARCHAR(80)	NOT NULL	COMMENT '저장 내용 해시 — 같으면 다시 쓰지 않음',
	`synced_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP	COMMENT '마지막 저장 시각',
	PRIMARY KEY (`owner_user_id`, `message_id`),
	KEY `ix_mail_owner_thread` (`owner_user_id`, `thread_id`),
	KEY `ix_mail_owner_sent` (`owner_user_id`, `sent_at`)
);

CREATE TABLE `mail_sync_state` (
	`owner_user_id`	BIGINT	NOT NULL	COMMENT '직원 ID',
	`last_sync_end`	DOUBLE	NULL	COMMENT '마지막으로 끝까지 성공한 동기화 창의 끝 (epoch 초)',
	`window_start`	DOUBLE	NULL	COMMENT '진행 중인 창 시작',
	`window_end`	DOUBLE	NULL	COMMENT '진행 중인 창 끝',
	`checkpoint`	JSON	NULL	COMMENT '진행 중 체크포인트 (다음 페이지 토큰) — NULL 이면 진행 중 아님',
	`job_id`	VARCHAR(64)	NULL	COMMENT '진행 중 작업 ID',
	PRIMARY KEY (`owner_user_id`)
);

CREATE TABLE `mail_sync_failure` (
	`id`	BIGINT	NOT NULL	AUTO_INCREMENT,
	`owner_user_id`	BIGINT	NOT NULL,
	`job_id`	VARCHAR(64)	NOT NULL,
	`message_id`	VARCHAR(64)	NULL,
	`reason`	VARCHAR(30)	NOT NULL	COMMENT 'fetch_error / too_large / invalid_message',
	`detail`	TEXT	NULL,
	`created_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (`id`)
);

CREATE TABLE `mail_sync_run` (
	`id`	BIGINT	NOT NULL	AUTO_INCREMENT,
	`owner_user_id`	BIGINT	NOT NULL,
	`job_id`	VARCHAR(64)	NOT NULL,
	`mode`	VARCHAR(20)	NOT NULL	COMMENT 'incremental / full / prune',
	`summary`	JSON	NOT NULL	COMMENT '건수 · 사유별 실패 수',
	`finished_at`	DATETIME	NOT NULL	DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (`id`)
);

ALTER TABLE `mail_account` ADD CONSTRAINT `FK_users_TO_mail_account` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`);
ALTER TABLE `mail_message` ADD CONSTRAINT `FK_users_TO_mail_message` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`);
