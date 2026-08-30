FROM python:3.12-slim
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && groupadd -r lab && useradd -r -g lab lab

# 非 root 运行（M0 安全模型的 Sandbox 要求）。
USER lab
EXPOSE 8090
ENV SYNTHETIC_LAB_PORT=8090
CMD ["python", "-m", "uvicorn", "synthetic_lab.main:app", "--host", "0.0.0.0", "--port", "8090"]
