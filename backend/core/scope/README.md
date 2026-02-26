# Scope 자동 감지 + Scope별 파생 엔진

> **IPCC 2006/2019 가이드라인을 코드로 구현한 온실가스 배출량 파생 엔진** 🌍  
> **AI 어시스턴트 활용** — 규정 문서를 직접 해석하고 AI와 협업하여 1,400+ 라인의 파생 엔진을 구현했습니다.

---

## 🧩 구성

```
scope/
├── detector.py        # OCR KV → Scope 유형 자동 감지
└── scope1/
    ├── derive.py      # Scope 1 파생 엔진 (1,400+ 라인)
    └── monthly.py     # 월별 연료 데이터 자동 추출·계산 엔진
```

---

## Scope 자동 감지

### 해결한 문제

업로드된 문서가 Scope 1인지, 그중 모바일 연소인지 고정 연소인지 **사용자가 직접 선택하지 않아도** 자동으로 판별합니다.

### 감지 방식

정규식 패턴 매칭으로 OCR 추출 KV의 연료명, 시설명, 단위를 분석합니다:

```python
_MOBILE_CONTEXT_PATTERN = re.compile(
    r"mobile\s*combustion|vessel|ship|aircraft|truck|lorry|vehicle|ferry",
    re.IGNORECASE,
)
_MOBILE_FUEL_PATTERN = re.compile(
    r"mgo|hfo|jet\s*fuel|aviation\s*fuel|kerosene|avgas",
    re.IGNORECASE,
)
_NG_CONTEXT_PATTERN = re.compile(
    r"natural\s*gas|lng|lpg|boiler|furnace|burner",
    re.IGNORECASE,
)
```

### 감지 우선순위

1. kv에 이미 유효한 JSON 배열 형식의 `Scopes Included` 값 존재 → 그대로 반환
2. Scope 2 키워드 또는 전력 단위(kWh/MWh) 감지
3. Scope 3 키워드 감지
4. 이동 연소 컨텍스트/연료명 감지 → `["scope_1", "mobile_combustion"]`
5. 디젤/경유 감지 → `["scope_1", "stationary_diesel"]`
6. 천연가스/보일러 또는 Nm³ 단위 감지 → `["scope_1", "stationary_natural_gas"]`

---

## Scope 1 파생 엔진

### 핵심 성과

**OCR로 읽은 연료 사용량과 배출계수 2개 값만 있으면 50개 이상의 필드를 자동 파생**합니다.  
사람이 일일이 채워야 했던 IPCC 기준 보고 항목들을 규칙 엔진이 자동으로 처리합니다.

### Source Type별 자동 기본값

연소 유형에 따라 적합한 기본값을 자동 주입합니다:

```python
_SOURCE_TYPE_DEFAULTS = {
    "stationary_ng": {
        "Usage Unit":              "Nm³",
        "Data Source":             "Flow Meter",
        "Data Collection Process": "Monthly cumulative meter reading",
        "Outlier Rule":            "Meter jump detection (>3σ from monthly mean)",
        "Missing Data Rule":       "Linear interpolation",
        "Activity Uncertainty":    "±2% (flow meter, Tier 1 default)",
        "EF Uncertainty":          "±5% (IPCC Tier 1 default EF)",
    },
    "stationary_diesel": {
        "Usage Unit":              "L",
        "Data Source":             "Fuel Purchase Record",
        "Outlier Rule":            "Invoice quantity mismatch check",
        ...
    },
    "mobile": {
        "Usage Unit":              "L",
        "Data Source":             "Fuel Receipt",
        ...
    },
}
```

### 주요 파생 로직

**① 인벤토리 연도 ↔ 기간 날짜 상호 추론**

```
Start Date + End Date → Inventory Year (연도 자동 추출)
Inventory Year만 있을 경우 → Start Date: 1월 1일, End Date: 12월 31일 자동 생성
```

**② 배출량 자동 계산 (E = AD × EF)**

```python
emission_kg = annual_fuel * ef_value   # 연료 × 배출계수
emission_t  = emission_kg / 1000       # kgCO₂e → tCO₂e 환산
```

**③ 불확도 전파 (Propagation of Error)**

```python
# IPCC Vol.1 Ch.3 방법론
combined_unc = math.sqrt(u_ad**2 + u_ef**2)
```

**④ QAQC 연쇄 자동완성**

이상치 규칙(Outlier Rule)과 결측 처리 규칙(Missing Data Rule)은 서로 보완 관계:
한쪽만 있으면 나머지를 자동 매핑합니다.

```python
_DEFAULT_QAQC_PAIRS = [
    ("3σ",       "Linear interpolation"),
    ("3-sigma",  "Linear interpolation"),
    ("iqr",      "Monthly mean substitution"),
]
```

**⑤ Scopes Excluded 자동 생성**

현재 보고 Scope 외 나머지를 자동으로 `Scopes Excluded` 필드에 기입합니다.

### 반환 형식

```python
{
    "Annual Emissions (tCO₂e)": {
        "value": "26.875",
        "source_ref": "E = AD × EF = 12,500 Nm³ × 2.15 kgCO₂e/Nm³ ÷ 1000"
    },
    "Combined Uncertainty": {
        "value": "±5.39%",
        "source_ref": "√(2%² + 5%²) = 5.39%  [IPCC Vol.1 Ch.3]"
    },
    ...
}
```

`source_ref`에 **계산 근거가 문자열로 포함**되어, UI에서 사용자가 어떤 기준으로 값이 계산됐는지 투명하게 확인 가능합니다.

---

## 월별 데이터 자동 추출 엔진

OCR이 추출한 CSV 파일에서 **월별 연료 소비량 컬럼을 자동으로 인식·파싱**합니다.

- 다양한 컬럼명 패턴 지원 (`Fuel Consumption (Nm³)`, `Fuel Usage`, `Energy Consumption` 등)
- 단위 자동 감지 (Nm³, L, kWh, kg, MJ 등)
- ERP 대사(Reconciliation) 데이터와 자동 교차 검증
- 결과를 `mrv_activity_data_monthly` 형식으로 반환
