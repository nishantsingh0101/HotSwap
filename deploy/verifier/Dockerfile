FROM python:3.12.8-slim
RUN pip install --no-cache-dir pytest==8.3.4
ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/workspace
WORKDIR /workspace
