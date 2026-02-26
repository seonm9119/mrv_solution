# 공통 유틸리티

> 파일 변환 · 필드 별칭 매핑 · 로거 · SQL 헬퍼 🛠️  
> **AI 어시스턴트 활용** — 한/영 200개 이상의 필드 별칭을 직접 정의하고 우선순위 스코어링 체계를 설계했습니다.

---

## 🧩 구성

```
utils/
├── keymap.py          # 필드 별칭(alias) 정의 + 파일명→keymap 매핑
├── to_png.py          # 다양한 파일 포맷 → PNG 변환
├── convert_excel.py   # Excel/CSV KV 직접 추출
├── helpers.py         # kv_dict 등 공통 헬퍼
├── scopes.py          # Scope 분류 상수
├── logger.py          # 구조화 로거 설정
└── sql.py             # SQL 쿼리 헬퍼
```

---

## 필드 별칭 매핑 (핵심 구현)

### 해결한 문제

서로 다른 문서에서 같은 데이터가 다른 이름으로 나타납니다:
- `"보고연도"` = `"Inventory Year"` = `"Reporting Year"` = `"FY"` = `"대상연도"`

OCR이 어떤 표현으로 추출하더라도 **동일한 논리 필드 이름(logical name)으로 정규화**해야 합니다.

### 구현 방식

각 논리 필드에 대해 한국어·영어 별칭을 모두 선언:

```python
INVENTORY_YEAR = [
    "인벤토리연도", "대상연도", "보고연도", "산정연도", "연도", "년도",
    "Inventory Year", "Reporting Year", "Report Year", "Target Year",
    "Year", "FY", "Fiscal Year",
]

FACILITY = [
    "설비", "설비명", "대상설비", "배출시설", "배출원", "공정",
    "Facility", "Equipment", "Plant Unit", "Emission Source",
    "보일러", "가열로", "발전기",
    "Boiler", "Heater", "Furnace", "Generator",
]
```

### 파일명 → Keymap 자동 매핑

업로드 파일명 패턴으로 어떤 keymap을 사용할지 자동 결정:

```python
FILENAME_TO_KEYMAP = {
    "annual_energy_fuel_report": [...],   # 연간 에너지 보고서
    "emission_factor_reference": [...],   # 배출계수 참조 문서
    "fuel_usage_raw_monthly":    [...],   # 월별 연료 소비 원데이터
    "mrv_report":                [...],   # MRV 보고서 메인
    "invoice":                   [...],   # 연료 구매 영수증
}
```

### 우선순위 스코어링

같은 논리 필드를 여러 문서에서 추출했을 때 **소스별 신뢰도 순위**로 최적값 선택:

```python
FIELD_SOURCE_PRIORITY = {
    "Emission Factor": [
        "emission_factor_reference",   # 공식 배출계수표 최우선
        "annual_energy_fuel_report",   # 연간 보고서 차순위
        "mrv_report",                  # MRV 보고서 그 다음
    ],
}
```

---

## 다중 포맷 PNG 변환

하나의 인터페이스로 모든 파일 포맷을 PNG 배열로 변환:

```python
def file_to_png_images(file_bytes, filename) -> List[PIL.Image]:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":     return _pdf_to_pngs(file_bytes)
    elif ext in (".xlsx", ".xls"):  return _excel_to_pngs(file_bytes)
    elif ext == ".docx":  return _docx_to_pngs(file_bytes)
    elif ext == ".hwp":   return _hwp_to_pngs(file_bytes)
    elif ext in (".png", ".jpg", ".jpeg"):  return [Image.open(...)]
```

---

## 구조화 로거

서비스별 이름을 가진 로거를 일관된 포맷으로 생성:

```python
def setup_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(name)s %(levelname)s — %(message)s"
    ))
    ...
```

모든 서비스(write_deepseek, mrv_write, llm 등)가 동일한 포맷으로 로그를 출력하여 Docker 로그에서 출처를 즉시 식별할 수 있습니다.
