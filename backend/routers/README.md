# routers/ — API 엔드포인트 레이어

> FastAPI `APIRouter` 기반 — HTTP 요청 파싱·응답 직렬화만 담당, 비즈니스 로직 없음  
> **바이브 코딩 활용** — API 설계부터 SSE 스트리밍 응답까지 전 엔드포인트를 직접 구현

---

## 설계 원칙

라우터는 **얇은 컨트롤러** 역할만 수행합니다:
- 요청 파라미터 검증
- `services/`, `core/` 호출
- 응답 포맷 직렬화

도메인 로직(파생 계산, KV 검증 등)은 절대 라우터에 두지 않았습니다.

---

## 구성

```
routers/
├── health.py          # 헬스체크
├── dashboard.py       # 대시보드 데이터 API
└── mrv/
    ├── mrv.py         # MRV 라우터 통합 (prefix: /api/mrv)
    ├── write.py       # MRV 작성 — 문서 업로드·추출·확정
    ├── report.py      # MRV 보고서 생성 (LLM SSE 스트리밍)
    ├── verification.py# 검증 데이터 조회
    └── default_kv.py  # MRV 기본값 상수
```

---

## mrv/write.py — MRV 작성 플로우

MRV 작성 페이지의 3단계 워크플로를 담당합니다.

### Step 1: 더미/실제 문서 업로드 및 OCR 추출

```
POST /api/mrv/extract
  multipart: files[], scope_type, report_id

  → 각 파일을 PNG로 변환
  → DeepSeek OCR API 호출 (비동기 병렬)
  → 마크다운 테이블 → CSV 저장
  → KV 추출 + Scope 자동 감지
  → derive_scope1() 파생 계산
  → validate_and_merge() 교차검증
  → 결과 반환
```

### Step 2: KV 검토 (프론트 UI에서 사람이 검토)

프론트엔드에서 `FLAGGED` 항목을 사람이 직접 수정합니다.

### Step 3: KV 확정 및 DB 저장

```
POST /api/mrv/confirm
  body: { kv: {...}, report_id, scope_type, files: [...] }

  → mrv_save.save_mrv_submission() 호출
  → PostgreSQL에 구조화 저장
  → MinIO에 원본 파일 보관
```

### 더미 폴더 지원

개발/데모 환경에서 실제 파일 없이도 동작하도록 `dummy/` 폴더를 스캔해 시나리오를 제공:

```python
GET /api/mrv/dummy-folders
→ { "scope1/Mobile(Aircraft)": [...files], "scope1/Stationary(NG)": [...] }
```

---

## dashboard.py — 대시보드 데이터 API

PostgreSQL의 대시보드 테이블에서 데이터를 조회해 프론트엔드에 제공합니다.

| 엔드포인트 | 테이블 | 반환 데이터 |
|---|---|---|
| `GET /api/dashboard/kpi` | `kpi_cards` | KPI 카드 (총 배출량, 감축률 등) |
| `GET /api/dashboard/trend` | `emission_trend` | 월별 배출량 추세 |
| `GET /api/dashboard/scope-breakdown` | `scope_breakdown` | Scope별 비중 |
| `GET /api/dashboard/category` | `category_emission` | 카테고리별 배출량 |
| `GET /api/dashboard/recent-pcf` | `recent_pcf` | 최근 PCF 목록 |

---

## mrv/report.py — SSE 스트리밍 보고서 생성

LLM 보고서 생성은 수십 초가 걸리므로 **Server-Sent Events로 진행 상황을 실시간 스트리밍**합니다:

```python
async def generate_events():
    yield "data: {\"status\": \"starting\"}\n\n"
    # 15개 LLM 모듈 순차 호출
    for module in report_modules:
        result = await module.resolve_llm_tag_async(tag, db_ctx)
        yield f"data: {json.dumps({'field': ..., 'value': result})}\n\n"
    yield "data: {\"status\": \"done\"}\n\n"

return StreamingResponse(generate_events(), media_type="text/event-stream")
```

클라이언트는 각 필드가 생성될 때마다 즉시 화면에 표시합니다.
