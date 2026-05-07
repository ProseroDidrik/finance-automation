# Multi-stage build för App Service for Containers.
#
# Stage 1: bygger React/Vite-frontend → webapp/frontend/dist/
# Stage 2: kopierar dist + Python-koden, kör FastAPI under uvicorn
#
# Allt i en image; FastAPI servar både API:et och den statiska bundlen
# via StaticFiles-mounten i webapp/backend/main.py.

# ----- Stage 1: frontend ------------------------------------------------------
FROM node:20-slim AS frontend
WORKDIR /build
COPY webapp/frontend/package.json webapp/frontend/package-lock.json ./
RUN npm ci
COPY webapp/frontend/ ./
RUN npm run build


# ----- Stage 2: app -----------------------------------------------------------
FROM python:3.12-slim AS app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WEBAPP_REQUIRE_FRONTEND=1

WORKDIR /app

# OS-paket: ingen extra binär krävs för psycopg[binary] eller openpyxl.
# Inget apt-get här håller image:n liten (~140 MB).

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Backend-källkod. _params/, _inbox/, data/, .venv etc. exkluderas via
# .dockerignore — webapp:en läser dim_company från Postgres och master-filen
# (om den behövs) från Blob Storage.
COPY db.py shared.py ./
COPY webapp/backend/ ./webapp/backend/
COPY webapp/config/  ./webapp/config/

# Frontend-bundle från stage 1
COPY --from=frontend /build/dist ./webapp/frontend/dist

# App Service for Containers: läs portnummer från $PORT om satt (default 8080).
# WEBSITES_PORT app setting måste matcha (default 80 → ändra till 8080).
ENV PORT=8080
EXPOSE 8080

# Kör som non-root av princip — App Service kräver det inte men vi får inte
# några fördelar med root-anslutning.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Lifespan-pingen i main.py kollar Postgres redan vid startup; uvicorn fail:ar
# då direkt om DATABASE_URL pekar fel. Ingen extra entrypoint-logik behövs.
CMD ["sh", "-c", "exec uvicorn webapp.backend.main:app --host 0.0.0.0 --port ${PORT}"]
