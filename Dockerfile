FROM python:3.11-slim

WORKDIR /app

# 의존성 먼저 복사 (Docker 레이어 캐싱 최적화)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 코드 복사
COPY app/ ./app/

# 환경변수는 docker run -e 로 주입 (예: -e OPENAI_API_KEY=sk-...)
# 기본 포트 8000
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
