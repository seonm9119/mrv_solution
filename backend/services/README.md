# 서비스 레이어 (외부 시스템 연동)

> 문서 추출 파이프라인 · MRV 저장 · MinIO 오브젝트 스토리지 🔗  
> **AI 어시스턴트 활용** — 6종 파일 포맷 처리, LaTeX 변환, MinIO 보안 처리까지 직접 설계·구현했습니다.

---

## 🔌 역할

서비스 레이어는 **외부 I/O를 캡슐화**합니다:
- OCR 서버 HTTP 호출
- PostgreSQL 저장 로직
- MinIO 파일 업로드/다운로드

라우터는 서비스만 호출하고, 서비스는 외부 시스템과 통신하여 `core` 비즈니스 로직과 분리됩니다.

---

## 🧩 구성

```
services/
├── extract.py    # 문서 → OCR → KV 전체 추출 파이프라인
├── mrv_save.py   # MRV 데이터 PostgreSQL 구조화 저장
└── storage.py    # MinIO 오브젝트 스토리지 연동
```

---

## 문서 추출 파이프라인

### 지원 파일 포맷

| 포맷 | 처리 방식 |
|---|---|
| PDF | PyMuPDF로 페이지별 PNG 변환 |
| Excel (.xlsx, .xls) | OpenPyXL/xlrd로 KV 직접 추출 + 시각화 PNG |
| CSV | pandas 없이 csv 모듈로 직접 파싱 |
| Word (.docx) | Mammoth으로 HTML 변환 → WeasyPrint로 PNG |
| HWP | PyHWP로 변환 |
| 이미지 (PNG, JPG) | 바로 OCR 입력 |

### 마크다운 테이블 파싱 및 CSV 저장

OCR이 반환하는 마크다운 텍스트에서 **3행×3열 이상 테이블을 자동 감지**하고 CSV로 저장합니다:

```python
def _save_large_tables_as_csv(raw_markdown, stem, page_idx, min_rows=3, min_cols=3):
    tables = _parse_md_tables(raw_markdown)
    for rows in tables:
        if len(rows) < min_rows or max_cols < min_cols:
            continue
        # LaTeX 수식 → Unicode 변환 후 CSV 저장
        csv_name = f"{stem}_{page_idx}_t{table_count}.csv"
        ...
```

저장된 CSV는 월별 데이터 엔진(`monthly.py`)이 자동으로 탐지하여 연료 소비량을 파싱합니다.

### LaTeX 수식 → Unicode 변환

OCR이 LaTeX로 인식한 수식을 가독성 있는 Unicode로 변환합니다:

```python
# LaTeX 아래첨자: \(_{n}\) → ₙ
s = re.sub(r"\\{1,2}\(_\{([^}]*)\}\\{1,2}\)",
           lambda m: m.group(1).translate(_SUBSCRIPT), s)

# LaTeX 위첨자: \(^{n}\) → ⁿ
s = re.sub(r"\\{1,2}\(\^\{([^}]*)\}\\{1,2}\)",
           lambda m: m.group(1).translate(_SUPERSCRIPT), s)
```

### Excel KV 직접 추출

Excel 파일은 이미지 변환 없이 **셀 데이터를 직접 파싱**하여 OCR 없이도 정확한 값을 추출합니다.  
인보이스(Invoice) 형식과 연간 보고서(Annual Report) 형식을 자동으로 구분하여 적합한 추출 전략을 선택합니다.

---

## MRV 구조화 저장

KV dict를 MRV 데이터 모델 구조에 맞게 **PostgreSQL 테이블에 분산 저장**합니다:

```
mrv_submission              ← 제출 메타데이터 (report_id, scope_type, 날짜)
  ↓
mrv_report                  ← 보고서 기본 정보 (조직, 경계 접근법)
mrv_activity_data           ← 활동자료 (연료 사용량, 기간, 집계 기준)
mrv_document_metadata       ← 문서 메타데이터 (준비자, 검토자, 승인자)
mrv_emission_factor_ref     ← 배출계수 참조 (EF값, 단위, 출처)
mrv_calculation_result      ← 계산 결과 (배출량, 불확도)
mrv_activity_data_monthly   ← 월별 활동자료 (12개월 레코드)
```

---

## MinIO 오브젝트 스토리지

원본 문서를 **S3 호환 MinIO에 안전하게 보관**합니다.

### 객체 키 구조

```
mrv/{mrv_id}/{document_id}/{uuid8}_{safe_filename}
```

경로 트래버설 공격 방지를 위해 파일명의 `..` 및 `/`를 제거합니다:

```python
safe_name = (file_name or "document").replace("..", "").strip()
if "/" in safe_name:
    safe_name = safe_name.split("/")[-1]
```

### Presigned URL

저장된 원본 문서는 Presigned URL로 임시 접근 제공 (기본 1시간):

```python
def get_presigned_url(bucket, object_key, expires_seconds=3600):
    from datetime import timedelta
    return client.presigned_get_object(
        bucket, object_key,
        expires=timedelta(seconds=expires_seconds),
    )
```
