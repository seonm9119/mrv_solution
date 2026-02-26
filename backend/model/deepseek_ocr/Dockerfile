# DeepSeek-OCR 전용 컨테이너: 빌드 시 모델 다운로드 후 로컬에서 추론
# context: backend (..), dockerfile: model/deepseek_ocr/Dockerfile
ARG CUDA_VERSION=11.8.0
ARG PYTHON_VERSION=3.12
# flash-attn 빌드에 nvcc 필요 → runtime 대신 devel 이미지 사용
FROM nvidia/cuda:${CUDA_VERSION}-cudnn8-devel-ubuntu22.04 AS base

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    git build-essential \
    && rm -rf /var/lib/apt/lists/*

# pip 및 Python 패키지 (flash-attn은 별도 설치)
COPY model/deepseek_ocr/requirements.txt /app/requirements.txt
WORKDIR /app

# torch+torchvision(cu118) 한 번에 설치 후, requirements는 torch 제외 (PyPI 덮어쓰기 방지)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir torch==2.4.0 torchvision==0.19.0 --index-url https://download.pytorch.org/whl/cu118 && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir psutil && \
    pip install --no-cache-dir flash-attn==2.7.3 --no-build-isolation

# 빌드 시 config.deepseek_ocr_model 해당 모델 미리 다운로드 (GPU 전용, /app/model)
# gated 모델이면: docker build --build-arg HF_TOKEN=your_token ...
ENV MODEL_DIR=/app/model
# Jalea96 bnb-4bit 사전양자화 모델 (~3.5GB 다운로드, 런타임 변환 불필요)
# masked_scatter_ monkey-patch + transformers 버전 고정으로 이전 호환 문제 해결.
ARG DEEPSEEK_OCR_MODEL_ID=Jalea96/DeepSeek-OCR-bnb-4bit-NF4
ENV DEEPSEEK_OCR_MODEL_ID=${DEEPSEEK_OCR_MODEL_ID}
ARG HF_TOKEN=
ENV HF_TOKEN=${HF_TOKEN}
RUN python3 -c "\
import os;\
from huggingface_hub import snapshot_download;\
snapshot_download(\
    repo_id=os.environ.get('DEEPSEEK_OCR_MODEL_ID', 'deepseek-ai/DeepSeek-OCR'),\
    local_dir='/app/model',\
    local_dir_use_symlinks=False,\
    token=os.environ.get('HF_TOKEN') or None\
)"

# 앱 코드
# utils/: 메인 백엔드와 공유하는 단일 유틸리티 패키지 (logger, keymap 등)
# refine.py: deepseek_ocr 전용 후처리 (utils/ 패키지 안에 배치)
COPY utils/ /app/utils/
COPY model/deepseek_ocr/utils/refine.py /app/utils/refine.py
COPY model/deepseek_ocr/extractor.py /app/extractor.py
COPY model/deepseek_ocr/server.py /app/server.py

ENV PYTHONPATH=/app
ENV PORT=4000
EXPOSE 4000

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "4000"]
