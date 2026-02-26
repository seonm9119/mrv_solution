# AI 모델 서버 클라이언트

> **DeepSeek OCR** (문서 인식) + **Qwen2.5-7B** (보고서 자동 작성) — 각각 독립 GPU 컨테이너로 격리  
> **바이브 코딩 활용** — AI 개발자로서 모델 내부(dtype 패치, VRAM 관리, 양자화)까지 직접 이해하고 제어

---

## 아키텍처 결정

AI 모델 2개를 **메인 백엔드와 별도 컨테이너**로 분리한 이유:

1. **VRAM 격리** — OCR 모델과 LLM을 같은 프로세스에서 실행하면 VRAM 충돌 발생
2. **독립 스케일링** — 문서 처리량과 보고서 생성량은 독립적으로 확장 가능
3. **모델 교체 유연성** — 메인 백엔드 코드 변경 없이 모델만 교체 가능

```
backend (FastAPI) ──httpx──→ deepseek_ocr (:8002)  [GPU 컨테이너 1]
                  ──httpx──→ qwen2x5_7b   (:8003)  [GPU 컨테이너 2]
```

---

## 구성

```
model/
├── deepseek_ocr/         # OCR 서버 (이미지 → 텍스트)
│   ├── server.py         # FastAPI 서버 진입점
│   ├── extractor.py      # DeepSeekOCR 추론 클래스
│   ├── docker-compose.yml
│   └── utils/
│       └── refine.py     # OCR 후처리 (마크다운 정제)
└── qwen2x5_7b/           # LLM 서버 (보고서 텍스트 생성)
    ├── common/
    │   └── common.py     # 공통 LLM 호출 유틸
    ├── mrv/              # MRV 보고서 15개 생성 모듈
    └── docker-compose.yml
```

---

## DeepSeek OCR

### 핵심 구현: 4-bit 양자화 모델 + dtype 패치

사전양자화(pre-quantized) 모델을 사용하여 `BitsAndBytesConfig` 없이 자동 4-bit 로딩:

```python
self._model = AutoModel.from_pretrained(
    self._model_dir,
    device_map="auto",
    _attn_implementation="flash_attention_2",  # Flash Attention 2로 속도 향상
    trust_remote_code=True,
    low_cpu_mem_usage=True,
)
```

DeepSeek OCR의 비전 인코더(float32)와 LLM 임베딩(bfloat16) 간 **dtype 불일치**를 패치로 해결:

```python
_original_masked_scatter_ = torch.Tensor.masked_scatter_

def _safe_masked_scatter_(self, mask, source):
    if self.dtype != source.dtype:
        source = source.to(self.dtype)  # dtype 자동 캐스팅
    return _original_masked_scatter_(self, mask, source)

torch.Tensor.masked_scatter_ = _safe_masked_scatter_
```

### VRAM 관리

추론 완료 후 모델을 즉시 언로드하여 다음 요청을 위한 VRAM을 확보:

```python
def _unload(self):
    self._model = None
    self._tokenizer = None
    gc.collect()
    torch.cuda.empty_cache()
```

### OCR 출력 후처리 (refine.py)

- 마크다운 볼드(`**...**`) / 이탤릭 제거
- LaTeX 수식(`\(_{n}\)`) → Unicode 아래첨자(`₀₁₂₃₄₅₆₇₈₉`) 변환
- 인라인 LaTeX 잔여 제거

---

## Qwen2.5-7B LLM

### 모듈형 보고서 생성 파이프라인

MRV 보고서의 각 섹션을 **독립 모듈**로 분리하여 15개 파일로 구현했습니다.  
각 모듈은 `{{llm:필드명|ref=db:키1,db:키2}}` 태그가 보고서 템플릿에서 발견될 때 비동기 호출됩니다.

| 모듈 파일 | 생성 내용 |
|---|---|
| `yoy_analysis.py` | 전년 대비(YoY) 배출량 변화 서술 |
| `boundary_justification.py` | 조직 경계 설정 근거 |
| `boundary_narrative.py` | 경계 서술 본문 |
| `scope_narrative.py` | Scope 포함/제외 서술 |
| `qc_note_activity.py` | 활동자료 품질관리(QC) 메모 |
| `qc_note_ef.py` | 배출계수 QC 메모 |
| `qc_note_uncertainty.py` | 불확도 QC 메모 |
| `fuel_reduction_levers.py` | 연료 절감 방안 제안 |
| `industry_benchmark.py` | 업계 벤치마크 비교 |
| `cost_reduction_analysis.py` | 비용 절감 분석 |
| `monthly_spike_analysis.py` | 월별 이상 급등 분석 |
| `reconciliation_checks.py` | 대사(Reconciliation) 검증 서술 |
| `dqi_basis.py` | 데이터 품질 지수(DQI) 근거 |
| `reporting_principles.py` | ISO 14064 보고 원칙 서술 |
| `declarations.py` | 준수 선언문 |

### 한국어 품질 보장

모든 LLM 호출에 한국어 전용 규칙 주입 + 중국어(한자) 필터링:

```python
_KOREAN_ONLY_INSTRUCTION = (
    "You are a professional Korean-language technical writer. "
    "All your responses must be written entirely in Korean (한국어). "
    "Use formal Korean (존댓말/격식체). "
    "Never mix in any foreign script."
)
```

Qwen 모델이 간혹 한자를 섞어 응답하는 문제를 CJK 유니코드 블록 기반 정규식으로 제거:

```python
_CJK_PATTERN = re.compile(
    "[\u4E00-\u9FFF\u3400-\u4DBF...]+"  # CJK 통합 한자 블록들
)
```
