# Build the UI, then serve it from the Python app so there is one origin,
# one port and one deploy.
FROM node:20-slim AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/ ./backend/
COPY --from=ui /ui/dist ./frontend/dist

WORKDIR /srv/backend
EXPOSE 8000

# Honour $PORT when the platform injects one (Render, Railway, DigitalOcean and
# Cloud Run all do; Cloud Run fails the deploy outright without it). Fly sets
# the port in fly.toml instead, so the 8000 fallback covers it and local runs.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
