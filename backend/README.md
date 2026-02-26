# Backend — FastAPI 기반 MRV 자동화 API 서버

> **Python 3.11 + FastAPI** | 비동기 PostgreSQL | MinIO 연동 | AI 파이프라인 오케스트레이션  
> **바이브 코딩 활용** — AI 개발자가 AI를 개발 파트너로 삼아 백엔드 전 영역을 직접 구현

---

## 역할과 설계 철학

백엔드는 세 가지 독립된 관심사를 명확히 분리하여 설계했습니다.

1. **API Layer** (`routers/`) — HTTP 요청 수신 및 응답 직렬화만 담당
2. **Business Logic** (`core/`) — Scope 파생 엔진, KV 검증 등 도메인 규칙 집중
3. **Infrastructure** (`services/`, `utils/`) — 외부 시스템(OCR, DB, MinIO) 연동

이 구조 덕분에 파생 엔진 로직을 라우터 코드와 완전히 독립시켜 **단독 테스트 및 재사용**이 가능합니다.

---

## 주요 구현 포인트

### FastAPI Lifespan 기반 DB 풀 관리

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()   # 앱 시작 시 asyncpg 커넥션 풀 생성
    yield
    await close_pool() # 종료 시 정상 해제
```

`asyncpg` 커넥션 풀을 `lifespan` 컨텍스트로 관리하여 요청마다 새 연결을 열지 않고, **고성능 비동기 DB 접근**을 구현했습니다.

### 비동기 OCR 실행

OCR은 CPU/GPU 집약적 작업이므로 FastAPI의 이벤트 루프를 블로킹하지 않도록 `run_in_executor`로 래핑했습니다:

```python
monthly_result = await loop.run_in_executor(
    None, lambda: build_monthly_data(df_dir, ef=ef_val)
)
```

### SSE(Server-Sent Events) 스트리밍 응답

LLM 보고서 생성처럼 수초~수십 초가 걸리는 작업은 `StreamingResponse`로 진행 상황을 실시간 스트리밍합니다. 클라이언트는 완료를 기다리지 않고 진행 상황을 즉시 표시합니다.

---

## 디렉토리 구조

```
backend/
├── main.py            # FastAPI 앱 진입점, 라우터 등록, lifespan 관리
├── config.py          # 환경변수 기반 설정 (Pydantic Settings)
├── database.py        # asyncpg 커넥션 풀 싱글턴
├── core/              # 도메인 비즈니스 로직
├── model/             # AI 모델 서버 클라이언트 (OCR, LLM)
├── routers/           # API 엔드포인트
├── services/          # 문서 추출, DB 저장, MinIO 연동
├── utils/             # 공통 유틸리티
├── static/            # 정적 파일
├── Dockerfile
└── requirements.txt
```

---

## 기술 스택

| 기술 | 용도 |
|---|---|
| **FastAPI** | REST API 프레임워크, 자동 OpenAPI 문서 |
| **asyncpg** | 비동기 PostgreSQL 드라이버 |
| **httpx** | 비동기 HTTP 클라이언트 (OCR/LLM 서버 호출) |
| **PyMuPDF (fitz)** | PDF → PNG 변환 |
| **Pillow** | 이미지 처리 |
| **OpenPyXL / xlrd** | Excel 파일 파싱 |
| **WeasyPrint** | HTML → PDF 변환 |
| **Mammoth** | .docx → HTML 변환 |
| **minio** | MinIO Python SDK |

---

## API 엔드포인트 요약

| 경로 | 기능 |
|---|---|
| `GET /api/health` | 헬스체크 |
| `GET /api/dashboard/*` | 대시보드 KPI, 차트 데이터 |
| `GET /api/mrv/default-kv` | MRV 기본 KV 값 반환 |
| `GET /api/mrv/dummy-folders` | 더미 시나리오 폴더 목록 |
| `POST /api/mrv/extract` | 문서 업로드 → OCR 추출 |
| `POST /api/mrv/confirm` | KV 확정 → DB 저장 |
| `POST /api/mrv/report` | MRV 보고서 LLM 생성 (SSE) |
| `GET /api/mrv/verification/*` | 검증 데이터 조회 |
