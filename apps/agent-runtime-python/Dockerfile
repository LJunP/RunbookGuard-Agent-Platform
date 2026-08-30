FROM python:3.12-slim
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && groupadd -r runtime && useradd -r -g runtime runtime

# 非 root 运行（M0 安全模型的 Sandbox 要求）。
USER runtime
EXPOSE 8100
# 默认 fake provider：镜像启动不会产生任何真实 API 调用。
ENV RUNBOOKGUARD_LLM_PROVIDER=fake
CMD ["python", "-m", "uvicorn", "agent_runtime.main:app", "--host", "0.0.0.0", "--port", "8100"]
