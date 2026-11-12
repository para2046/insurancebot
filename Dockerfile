FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=7860
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m appuser && chown -R appuser /app
USER appuser
EXPOSE 7860
# Gradio shuts down cleanly on Ctrl-C (SIGINT); the default SIGTERM would be ignored and force-killed
STOPSIGNAL SIGINT
# Pass the key at runtime:  docker run -e OPENROUTER_API_KEY=sk-or-... -p 7860:7860 insurance-sop-agent
CMD ["python", "app.py"]
