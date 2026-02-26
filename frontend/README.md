# Frontend — React 기반 MRV 워크플로 SPA

> **React 18 + Vite + Tailwind CSS + Recharts** | MRV 3단계 워크플로 UI 🖥️  
> **AI 어시스턴트 활용** — AI 개발자가 AI를 개발 파트너로 삼아 프론트엔드 전 영역을 직접 구현했습니다.

---

## ✨ 핵심 구현 포인트

### 1. MRV 3단계 워크플로 (Writing → Reporting → Verification)

```
/mrv/writing      문서 업로드 → OCR 추출 → KV 검토 → 확정 저장
/mrv/reporting    MRV 보고서 LLM 자동 생성 (SSE 실시간 스트리밍)
/mrv/verification 검증 데이터 조회
```

### 2. 실시간 LLM 스트리밍 표시

보고서 생성 중 각 필드가 완성되는 즉시 화면에 표시합니다.  
`EventSource` API로 백엔드 SSE 스트림을 구독:

```javascript
const es = new EventSource('/api/mrv/report/stream?report_id=...')
es.onmessage = (e) => {
  const { field, value, status } = JSON.parse(e.data)
  if (status === 'done') { es.close(); return }
  setReportFields(prev => ({ ...prev, [field]: value }))
}
```

### 3. 자동 계산 훅 (useAutoCompute)

KV 검토 화면에서 사용자가 연료 사용량이나 배출계수를 수정하면,  
`useAutoCompute` 커스텀 훅이 **백엔드 파생 API를 자동 재호출**하여 계산 결과를 즉시 업데이트합니다:

```javascript
// frontend/src/components/mrv/write/useAutoCompute.js
useEffect(() => {
  if (!fuelUsage || !emissionFactor) return
  const timer = setTimeout(() => {
    recomputeDerived({ fuelUsage, emissionFactor, scopeType })
      .then(setDerivedValues)
  }, 500)  // 500ms 디바운스
  return () => clearTimeout(timer)
}, [fuelUsage, emissionFactor])
```

---

## 📂 디렉토리 구조

```
frontend/
├── src/
│   ├── App.jsx                    # 라우팅 정의
│   ├── components/
│   │   ├── Dashboard.jsx          # 대시보드 메인
│   │   ├── dashboard/             # 대시보드 차트 컴포넌트
│   │   │   ├── KpiCards.jsx       # KPI 카드
│   │   │   ├── EmissionTrendChart.jsx  # 배출량 추세 (라인 차트)
│   │   │   ├── ScopeDonutChart.jsx     # Scope 비중 (도넛 차트)
│   │   │   ├── CategoryBarChart.jsx    # 카테고리별 (막대 차트)
│   │   │   └── RecentPcfList.jsx       # 최근 PCF 목록
│   │   ├── mrv/                   # MRV 워크플로 컴포넌트
│   │   │   ├── write/             # 작성 단계 세부 컴포넌트
│   │   │   │   ├── MRVWritingUploads.jsx    # 파일 업로드
│   │   │   │   ├── MRVWritingKvReview.jsx   # KV 검토
│   │   │   │   ├── useAutoCompute.js        # 자동 재계산 훅
│   │   │   │   ├── ReviewConfirmModal.jsx   # 확정 모달
│   │   │   │   └── SaveSuccessModal.jsx     # 저장 완료 모달
│   │   │   ├── MRVReporting.jsx   # 보고서 생성 (SSE 스트리밍)
│   │   │   └── MRVVerification.jsx# 검증 데이터 조회
│   │   ├── panels/                # 우측 패널 컴포넌트
│   │   ├── Sidebar.jsx            # 네비게이션 사이드바
│   │   ├── Breadcrumb.jsx         # 경로 표시
│   │   └── ErrorBoundary.jsx      # 에러 경계
│   └── main.jsx
├── tailwind.config.js
├── vite.config.js
└── Dockerfile
```

---

## 대시보드 구현

Recharts를 사용한 4종류 데이터 시각화:

| 컴포넌트 | 차트 유형 | 데이터 |
|---|---|---|
| `EmissionTrendChart` | LineChart | 월별 배출량 vs 목표치 |
| `ScopeDonutChart` | PieChart | Scope 1/2/3 비중 |
| `CategoryBarChart` | BarChart + ComposedChart | 카테고리별 배출량 vs 목표 |
| `KpiCards` | 카드 UI | 총 배출량, 감축률, 검증 건수 |

---

## MRV Writing 플로우

### Step 1 — 파일 업로드 (`MRVWritingUploads`)

- 더미 시나리오 폴더 선택 또는 실제 파일 드래그&드롭
- `POST /api/mrv/extract` 호출 → 로딩 스피너 표시
- 완료 후 KV 검토 화면으로 자동 전환

### Step 2 — KV 검토 (`MRVWritingKvReview`)

- 섹션별(활동자료, 배출계수, 계산 결과 등) KV 테이블 표시
- `FLAGGED` 항목 강조 표시 → 사람 직접 수정
- `DERIVED_WINS` 항목은 grey-out (편집 불가)
- 수정 시 `useAutoCompute`로 파생값 자동 재계산

### Step 3 — 확정 저장 (`ReviewConfirmModal`)

- 최종 KV 확인 모달
- `POST /api/mrv/confirm` 호출
- 저장 완료 후 `SaveSuccessModal` 표시

---

## UI/UX 설계

- **Tailwind CSS** 유틸리티 클래스로 일관된 디자인 시스템
- **ErrorBoundary** 컴포넌트로 런타임 에러 격리 (전체 화면 오류 방지)
- `MRVMonitoringLayout` 공통 레이아웃으로 MRV 페이지 헤더/빵 부스러기 일관화
- 반응형 그리드 레이아웃 (`flex-1 min-h-0 overflow-hidden` 패턴)
