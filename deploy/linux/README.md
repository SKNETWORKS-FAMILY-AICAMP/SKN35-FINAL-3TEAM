# 리눅스 서버 설치 (폐쇄망, Docker 없이)

은행 사내망의 리눅스 서버 한 대에 DB·앱을 직접 설치한다. 직원 PC 는 브라우저만 쓴다.
서버는 인터넷이 안 되므로, **같은 리눅스(배포판·버전)인 인터넷 되는 PC** 에서 설치 파일을 모아 반입한다.

```
[인터넷 되는 PC, 서버와 같은 OS]  ① 꾸러미 만들기
        ↓ USB·반입 절차 (sha256 으로 확인)
[사내 리눅스 서버]                ② PostgreSQL·pgvector → ③ 파이썬 패키지 → ④ DB 만들기 → ⑤ 문서 등록 → ⑥ 서비스 등록
[직원 PC]                         브라우저로 접속
```

필요한 것 한눈에

| 무엇 | 버전 | 어디서 |
|---|---|---|
| PostgreSQL | 16 | 배포판 패키지 또는 PostgreSQL 공식 저장소(PGDG) |
| pgvector | **0.8 이상** (halfvec 0.7+, 반복 탐색 0.8+) | PGDG 패키지 `postgresql-16-pgvector`(데비안·우분투) / `pgvector_16`(RHEL 계열) |
| Python | 3.12 | `uv python install 3.12` 로 받은 폴더째 반입 |
| 파이썬 패키지 | `uv.lock` 그대로 | 아래 ①-3 |
| 모델 | KURE-v3 폴더 (임베딩), 파서 모델 폴더 (PDF·이미지만) | 팀 공유 폴더 |
| LLM | Ollama + qwen3:8b | 별도 (이 문서 범위 밖) |

---

## ① 꾸러미 만들기 (인터넷 되는 PC)

우분투 22.04·24.04 기준 명령이다. RHEL 계열은 `dnf download --resolve` 로 같은 일을 한다.

1. **PostgreSQL 16 + pgvector 패키지** (의존 패키지까지)
   ```bash
   # PGDG 저장소를 등록한 뒤 (https://www.postgresql.org/download/linux/ubuntu/)
   mkdir -p bundle/debs && cd bundle/debs
   apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
       --no-breaks --no-replaces --no-enhances postgresql-16 postgresql-16-pgvector | grep '^\w' | sort -u)
   cd ../..
   ```
2. **Python 3.12**: `uv python install 3.12` 후 `uv python dir` 에 생긴 3.12 폴더와 `uv` 실행 파일을 `bundle/` 로 복사.
3. **파이썬 패키지** (저장소 맨 위에서)
   ```bash
   uv export --frozen --no-dev --no-hashes --no-emit-workspace -o bundle/requirements.txt
   uv run --with pip python -m pip download -r bundle/requirements.txt -d bundle/wheels
   ```
4. **코드와 모델**: 저장소(`git archive` 등), `KURE-v3` 폴더, (PDF·이미지 파싱을 쓰면) 파서 모델 폴더를 `bundle/` 에.
5. **확인값**: `cd bundle && find . -type f -exec sha256sum {} + > SHA256SUMS`

## ② PostgreSQL · pgvector 설치 (서버)

```bash
cd bundle && sha256sum -c SHA256SUMS --quiet
sudo apt-get install ./debs/*.deb          # 인터넷 없이 받은 파일로만 설치
sudo systemctl enable --now postgresql
psql --version                              # 16.x
```

## ③ 파이썬 패키지 (서버)

```bash
cd /opt/agent-platform                      # 코드 위치 (예시)
uv venv --python /opt/python3.12/bin/python3.12 .venv
uv pip install --offline --no-index --find-links bundle/wheels -r bundle/requirements.txt
```

## ④ DB 만들기 (서버)

```bash
cp .env.example .env                        # DATABASE_URL, EMBEDDING_MODEL_PATH, STORAGE_ROOT 등을 채운다
sudo -u postgres bash deploy/linux/setup_db.sh agent_platform agent_platform_app
```

`setup_db.sh` 가 하는 일: DB 만들기 → `CREATE EXTENSION vector` (0.8 이상인지 확인) → `alembic upgrade head`(0001~0005)
→ 앱 계정(감사 로그는 읽기·추가만) 만들기. 비밀번호는 실행 중에 직접 입력한다 (파일·기록에 남기지 않음).

이어서 개발용 사용자·부서를 넣는다 (로그인 기능이 붙기 전까지):

```bash
.venv/bin/python scripts/db/seed_users.py
```

## ⑤ 문서 등록 (서버)

```bash
.venv/bin/python scripts/db/register_doc.py /data/내규/ --category 내규 --owner admin --scope company --grade 1
```

원본은 `STORAGE_ROOT/documents/{문서}/v{버전}/` 에, 파싱 결과는 그 옆 `parsed/` 에 남는다.
KURE-v3 는 CPU 로 돈다 (내규 17개 기준 약 50분이 걸렸다 — 처음 한 번만).

## ⑥ 앱을 서비스로 등록 (서버)

`.env` 에 `DOC_STORE=db` 를 넣고 `deploy/linux/agent-platform.service` 를 복사해 경로를 맞춘다.

```bash
sudo cp deploy/linux/agent-platform.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now agent-platform
journalctl -u agent-platform -f            # "문서 도구: 팀 DB(pgvector) 사용" 이 보이면 연결됨
```

## 백업

- DB: `pg_dump -Fc agent_platform > 백업.dump` (매일), 되살리기 `pg_restore -d agent_platform 백업.dump`
- 원본 문서: `STORAGE_ROOT` 폴더 통째로

## 확인 상태 (2026-11-23)

개발 PC 의 WSL 우분투 26.04 에서 확인했다 (실제 은행 서버는 아직).

- ② PGDG 저장소로 PostgreSQL 16.16 + pgvector 0.8.7 설치 (인터넷 연결 상태에서 — 반입 꾸러미 방식 ①은 아직 안 해 봄)
- ④ `setup_db.sh`: DB · 확장 · 표(0001~0004, `chunk.embedding` = halfvec(2048)) · 앱 계정까지 끝남
- `seed_users.py` → `register_doc.py` 로 내규 HWP 1개(청크 24개)·FAQ HWPX 1개(청크 23개) 등록
- `DOC_STORE=db` 로 앱을 띄우면 문서 도구가 DB 를 쓴다. 검색 한 번 약 0.1~0.2초(CPU, 모델은 미리 올림, 첫 검색 약 1초)
- 권한: 부서 문서(dept)는 같은 부서 사용자만 검색·열람됨, 등급 없는 사용자는 아무것도 못 봄
- 확인 못 한 것: ③ 인터넷 없는 패키지 설치, ⑥ systemd 서비스, LLM(Ollama)까지 붙인 채팅 흐름
- 서버 배포판·버전은 미정 (우분투 기준으로 씀)
