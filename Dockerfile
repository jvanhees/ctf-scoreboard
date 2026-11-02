FROM ghcr.io/astral-sh/uv:0.9.5-python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
COPY app.py test_app.py ./
COPY templates templates
CMD ["python", "-u", "app.py"]
