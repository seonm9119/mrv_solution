# Python 3.8: pyhwp(HWP→HTML)가 3.5~3.8만 지원
FROM python:3.8-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # (옵션) 한글/로케일 안정화
    LANG=ko_KR.UTF-8 \
    LC_ALL=ko_KR.UTF-8

# Linux 우회: HWP/DOC/DOCX 변환용 (LibreOffice 없이 weasyprint 등)
RUN apt-get update && apt-get install -y --no-install-recommends \
    # .doc (Word 97-2003) → 텍스트 추출
    catdoc \
    # PDF → PNG (poppler: 한글 등 CJK fontconfig 대체)
    poppler-utils \
    poppler-data \
    # weasyprint (HTML→PDF) 런타임
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    shared-mime-info \
    fontconfig \
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    fonts-dejavu \
    fonts-liberation \
    locales \
    && sed -i 's/^# *\(ko_KR.UTF-8 UTF-8\)/\1/' /etc/locale.gen \
    && locale-gen \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

# PDF 한글: Windows/한컴 폰트명 → Noto Sans CJK 대체 (poppler가 fontconfig로 사용)
RUN printf '%s\n' '<?xml version="1.0"?>' '<fontconfig>' \
  '<match target="pattern"><test name="family"><string>Malgun Gothic</string></test><edit name="family" mode="assign" binding="strong"><string>Noto Sans CJK KR</string></edit></match>' \
  '<match target="pattern"><test name="family"><string>Batang</string></test><edit name="family" mode="assign" binding="strong"><string>Noto Serif CJK KR</string></edit></match>' \
  '<match target="pattern"><test name="family"><string>Dotum</string></test><edit name="family" mode="assign" binding="strong"><string>Noto Sans CJK KR</string></edit></match>' \
  '<match target="pattern"><test name="family"><string>Gulim</string></test><edit name="family" mode="assign" binding="strong"><string>Noto Sans CJK KR</string></edit></match>' \
  '<match target="pattern"><test name="family"><string>AppleGothic</string></test><edit name="family" mode="assign" binding="strong"><string>Noto Sans CJK KR</string></edit></match>' \
  '</fontconfig>' > /etc/fonts/local.conf && fc-cache -fv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 4000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "4000"]
