FROM python:3.12-slim
RUN pip install --no-cache-dir beautifulsoup4==4.12.3
WORKDIR /app
COPY app.py test_app.py ./
CMD ["python", "-u", "app.py"]
