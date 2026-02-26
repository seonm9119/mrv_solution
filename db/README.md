# db/ — PostgreSQL 데이터베이스 초기화

> Docker 컨테이너 시작 시 자동으로 스키마 생성 + 더미 데이터 삽입  
> **바이브 코딩 활용** — MRV 도메인을 분석하여 7테이블 정규화 구조를 직접 설계

---

## 구성

```
db/
├── Dockerfile
└── init/
    ├── 01-schema.sql                # 대시보드 테이블 스키마
    ├── 01-mrv.sql                   # MRV 데이터 모델 스키마
    ├── docker-entrypoint-wrapper.sh # PostgreSQL 초기화 진입점 래퍼
    └── run-all.sh                   # SQL 파일 순차 실행 스크립트
```

---

## 데이터 모델 설계

### 대시보드 테이블 (`01-schema.sql`)

단순 조회 목적의 테이블로 설계:

```sql
CREATE TABLE kpi_cards (
    id        SERIAL PRIMARY KEY,
    title     VARCHAR(100),
    value     VARCHAR(50),
    unit      VARCHAR(20),
    sub       VARCHAR(50),
    trend     VARCHAR(20),   -- "↑ 3.2%" 형식
    trend_up  BOOLEAN,
    icon      VARCHAR(10)    -- 이모지 아이콘
);

CREATE TABLE emission_trend (
    month    VARCHAR(20),
    emission DECIMAL(10,4),
    target   DECIMAL(10,4)   -- 감축 목표치와 비교
);

CREATE TABLE scope_breakdown (
    name  VARCHAR(50),
    value DECIMAL(5,2),      -- 비율 (%)
    color VARCHAR(20)        -- 차트 색상 코드
);
```

### MRV 데이터 모델 (`01-mrv.sql`)

MRV 제출을 **7개 테이블로 정규화**:

```
mrv_submission              ← 제출 단위 (1:N → 아래 모든 테이블)
  ├── mrv_report             ← 보고서 헤더 정보
  ├── mrv_activity_data      ← 활동자료 (연료 사용, 기간)
  │   └── mrv_activity_data_monthly  ← 월별 세부 데이터 (12행)
  ├── mrv_document_metadata  ← 문서 작성/검토/승인자 정보
  ├── mrv_emission_factor_ref← 배출계수 참조 데이터
  └── mrv_calculation_result ← 계산 결과 (배출량, 불확도)
```

이 구조 덕분에:
- 월별 데이터를 별도 테이블로 분리하여 집계 쿼리가 용이
- 배출계수 레퍼런스를 별도 관리하여 여러 제출에서 공유 가능
- LLM 보고서 생성 시 `db_ctx`로 관련 테이블을 조인하여 컨텍스트 구성

---

## 초기화 자동화

PostgreSQL 컨테이너는 `docker-entrypoint-wrapper.sh`가 SQL 파일을 번호 순서대로 자동 실행합니다:

```bash
# run-all.sh
for sql_file in /docker-entrypoint-initdb.d/init/*.sql; do
    psql -U $POSTGRES_USER -d $POSTGRES_DB -f "$sql_file"
done
```

`01-` 접두사로 실행 순서를 보장하며, 스키마 → 더미 데이터 순으로 안전하게 초기화됩니다.

---

## Docker Compose 헬스체크

백엔드가 DB 준비 완료 전에 연결을 시도하는 문제를 방지:

```yaml
postgres:
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U lca -d lca"]
    interval: 3s
    retries: 5

backend:
  depends_on:
    postgres:
      condition: service_healthy   # DB 헬스체크 통과 후에만 시작
```
