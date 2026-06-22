# Wdrożenie TaxPilot na Mikrusie

Pełny stack: **OpenSearch + PostgreSQL + Redis + Django (gunicorn)** za nginx.

## 0. Dobór maszyny

OpenSearch (JVM/Lucene) + embedder stella-pl-mini decydują o RAM:

- OpenSearch: ~1–1.5 GB (heap 512 MB + narzut)
- embedder (do zapytań w czasie rzeczywistym): ~1.5–2 GB
- Django + Postgres + Redis + system: ~1 GB

**Celuj w ≥ 4 GB RAM.** Diagnostyka maszyny:

```bash
bash deploy/check_mikrus.sh
```

OpenSearch w kontenerze LXC: `node.store.allow_mmap=false` w compose pozwala
wystartować bez zmiany `vm.max_map_count` (na KVM możesz włączyć mmap).

## 1. Usługi bazowe

**OpenSearch** (Docker, z pluginem Stempel):

```bash
cd deploy
docker compose up -d --build
curl -s localhost:9200/_cat/plugins | grep -i stempel
```

**PostgreSQL + Redis** — z repozytoriów albo Docker. Utwórz bazę i usera
zgodne z `.env` (POSTGRES_DB/USER/PASSWORD), Redis na 6379.

## 2. Aplikacja Django

```bash
cd /home/kwasiucionek/taxpilot
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # uzupełnij hasła, OLLAMA_CLOUD_API_KEY
#  w .env: POLISH_STEM_FILTER=polish_stem, OPENSEARCH_INDEX=taxpilot

python manage.py migrate
python manage.py createsuperuser
```

## 3. Ingest

Embeddingi liczone na CPU Mikrusa są wolne — najlepiej z lokalnej maszyny
(RTX 5090) tunelem SSH do OpenSearcha **i** Postgresa Mikrusa:

```bash
ssh -N -L 9200:127.0.0.1:9200 -L 5432:127.0.0.1:5432 user@pro01.mikr.us &
python manage.py ingest_acts --all --od 2024-01-01
```

Albo bezpośrednio na VPS (wolniej): to samo polecenie bez tunelu.
> `ingest_acts` jest synchroniczny — NIE wymaga always-on workera Celery.

## 4. Usługi systemd

```bash
cp deploy/taxpilot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now taxpilot          # Django/gunicorn na 127.0.0.1:8503

# opcjonalnie worker Celery (druga kopia stelli w RAM — patrz uwaga w pliku):
cp deploy/taxpilot-worker.service /etc/systemd/system/
systemctl enable --now taxpilot-worker
```

`taxpilot.service` sam robi `migrate` + `collectstatic` przy starcie.

## 5. nginx

```bash
cp deploy/nginx-taxpilot.conf /etc/nginx/sites-available/taxpilot
# dostosuj `listen` do portu z Mikrusa i `server_name` (taxpilot.cytr.us)
ln -s /etc/nginx/sites-available/taxpilot /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

OpenSearch słucha tylko na `127.0.0.1:9200` — nigdy nie wystawiaj go
(z wyłączonym security) do internetu.

## Warianty serwowania

- **Streamlit** (szybkie demo): `deploy/taxpilot-streamlit.service` +
  `deploy/nginx-taxpilot-streamlit.conf`.
- **FastAPI** (opcjonalne API, `api.py`): odkomentuj fastapi/uvicorn
  w `requirements.txt`.
