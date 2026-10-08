FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .
ENV SABZOMI_DB=/data/sabzomi.db SABZOMI_ENV=production SABZOMI_AUTORUN_JOBS=1 PORT=8000
VOLUME /data
EXPOSE 8000
# One worker + threads: SQLite allows a single writer, and the background job thread must run once.
CMD ["sh", "-c", "python seed.py && exec gunicorn -w 1 --threads 8 -b 0.0.0.0:${PORT} 'app:create_app()'"]
