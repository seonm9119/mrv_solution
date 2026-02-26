# 도메인 비즈니스 로직 레이어

> **KV 교차검증 엔진** + **Scope 자동 감지** — 순수 Python, 외부 의존성 없음 💡  
> **AI 어시스턴트 활용** — IPCC 가이드라인을 직접 분석하고 AI와 협업하여 검증 로직을 설계·구현했습니다.

---

## 🎯 설계 원칙

`core/` 폴더는 **외부 I/O가 전혀 없는 순수 비즈니스 로직**만 담습니다.  
FastAPI 라우터, 데이터베이스, AI 모델과 완전히 분리되어 있어 단독으로 테스트하거나 다른 프로젝트에서 재사용할 수 있습니다.

---

## 🧩 구성 파일

```
core/
├── kv.py         # KV 정규화 · 교차검증 · 충돌 해소 엔진
└── scope/        # Scope 자동 감지 + Scope별 파생 엔진
```

---

## KV 교차검증 엔진

### 해결한 문제

OCR로 문서를 읽으면 **값이 틀릴 수 있습니다** (스캔 품질, 폰트 인식 오류).  
반면, 규정상 일부 필드는 **시스템이 계산한 값이 항상 정확**해야 합니다 (배출계수 적용 결과, 단위 환산 등).  
두 출처의 값이 충돌할 때 **어떤 값을 최종으로 사용할지** 명확한 정책이 필요합니다.

### 해소 정책 (Resolution Policy)

세 가지 정책을 키별로 선언적으로 지정합니다:

```python
_KEY_POLICY = {
    # 시스템 계산값 고정 — UI grey-out 권장
    "Annual Emissions (tCO₂e)":  ResolutionPolicy.DERIVED_WINS,
    "Formula":                   ResolutionPolicy.DERIVED_WINS,

    # 불일치 시 사람이 반드시 검토
    "Emission":                  ResolutionPolicy.FLAG,
    "Inventory Year":            ResolutionPolicy.FLAG,

    # OCR 실측값 우선, 파생은 fallback
    "Start Date":                ResolutionPolicy.OCR_WINS,
    "Emission Factor":           ResolutionPolicy.OCR_WINS,
}
```

### 수치 허용오차 비교

단순 문자열 비교가 아닌 **1% 상대 오차 이내 수치를 동일로 판정**:

```python
_NUMERIC_TOLERANCE = 0.01  # 1%

def _values_match(a, b):
    if _normalize(a) == _normalize(b):
        return True
    na, nb = _extract_numeric(a), _extract_numeric(b)
    if na is not None and nb is not None:
        return abs(na - nb) / max(abs(nb), 1e-9) <= _NUMERIC_TOLERANCE
```

### OCR 노이즈 정규화

필드 특성에 맞는 정규화를 필드별로 분리 적용합니다:

| 필드 유형 | 정규화 처리 |
|---|---|
| Person 필드 (Approved By 등) | 파이프(`\|`) 이후 제거, 한글/영문 이름 패턴 인식, 기관명 오인식 방지 |
| Emission Factor | 단위 텍스트 분리(`2.15 kgCO₂e/Nm³` → 숫자만, 단위는 별도 키에 자동 주입) |
| 숫자 필드 | 천단위 콤마 자동 포맷 |
| Scope 복합값 | `"Scope 1 - Mobile Combustion"` → Scope / Activity Name 자동 분리 |

### 반환 구조

```python
@dataclass
class ValidationResult:
    final_kv:  Dict[str, str]          # 최종 확정 KV
    fields:    Dict[str, FieldResult]  # 키별 상세 결과 (status, ocr값, 파생값, 근거)
    conflicts: List[FieldResult]       # CONFLICT + FLAGGED 목록
    summary:   Dict[str, int]          # 상태별 카운트
```

---

## 공개 API

```python
from core.kv import normalize_ocr_kv, validate_and_merge, strip_derived_keys

# 1. OCR raw KV 정규화
normalized = normalize_ocr_kv(raw_kv, source_map)

# 2. 교차검증 (DERIVED_WINS 키 제거 후 파생 강제 실행)
driver_kv = strip_derived_keys(normalized)
derived   = derive_scope1(driver_kv)
result    = validate_and_merge(normalized, derived)

# 3. 결과 활용
print(result.final_kv)        # 최종 확정 KV
print(result.summary)         # {"confirmed": 32, "conflict": 2, "flagged": 1, ...}
```
