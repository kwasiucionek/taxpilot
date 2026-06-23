# Wdrożenie TaxPilot na Mikrusie

Pełny stack: **OpenSearch + PostgreSQL + Redis + Django (gunicorn)** za nginx.

## 0. Dobór maszyny

OpenSearch (JVM/Lucene) + embedder stella-pl-mini decydują o RAM:

- OpenSearch: ~1–1.5 GB (heap 512 MB + narzut)
- embedder (do zapytań w czasie rzeczywistym): ~1.5–2 GB
- Django + Postgres + Redis + system: ~1 GB
- (opcjonalnie) always-on worker Celery — druga kopia embeddera: ~1.5–2 GB

**Minimum ~4 GB RAM.** Przy **~16 GB** (np. `steve141`) spokojnie utrzymasz
always-on worker Celery + Beat (cały stack ~6–7 GB użytych). Diagnostyka:

```bash
bash deploy/check_mikrus.sh
free -m
```

> Uwaga: Mikrus bywa bez swap (`Swap: 0`) — przy ≥ 16 GB to bez znaczenia, ale
> unikaj nagłych skoków pod sufit.

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

> **Uwaga (tunel):** jeśli ingest robisz z lokalnej maszyny przez tunel SSH
> (sekcja 3), w `.env` kieruj usługi na `127.0.0.1`, **nie** `localhost` —
> `localhost` potrafi rozwiązać się na IPv6 (`::1`), a tunel binduje IPv4:
> `OPENSEARCH_URL=http://127.0.0.1:9200`, host Postgresa `127.0.0.1:5432`.

## 3. Ingest (trzy źródła)

Embeddingi liczone na CPU Mikrusa są wolne — dla pierwszego pełnego wsadu
najszybciej z lokalnej maszyny (RTX 5090) tunelem SSH do OpenSearcha **i**
Postgresa Mikrusa. To wybór szybkości, nie konieczność (na CPU też przejdzie,
tylko wolniej).

Mikrus używa **niestandardowego portu SSH** (sprawdź w panelu, np. `10141`):

```bash
ssh -N -o ServerAliveInterval=30 -p <PORT_SSH> \
    -L 9200:127.0.0.1:9200 \
    -L 5432:127.0.0.1:5432 \
    root@<serwer>.mikrus.xyz &
```

Na GPU możesz podbić batch embeddera (`EMBED_BATCH_SIZE=64` w środowisku).
Następnie zaindeksuj wszystkie trzy źródła:

```bash
# 1) Ustawy (ELI — resolver bierze najnowszy tekst jednolity):
python manage.py ingest_acts --all --od 2024-01-01

# 2) Objaśnienia MF (kuratorska lista PDF):
python manage.py ingest_objasnienia --all

# 3) Interpretacje KIS z EUREKA (ulga sama dobiera przepisy):
python manage.py ingest_interpretacje --ulga BR    --limit 50 --od-daty 2023-01-01
python manage.py ingest_interpretacje --ulga IPBOX --limit 50 --od-daty 2023-01-01
python manage.py ingest_interpretacje --ulga PKUP  --limit 50 --od-daty 2023-01-01
```

Ingest jest idempotentny (`_id = doc_id`), więc można go bezpiecznie ponawiać —
jeśli tunel padnie w połowie, po prostu odpal ponownie. Te same komendy działają
też wprost na VPS (bez tunelu).

## 4. Usługi systemd

```bash
cp deploy/taxpilot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now taxpilot          # Django/gunicorn na 127.0.0.1:8503
```

`taxpilot.service` sam robi `migrate` + `collectstatic` przy starcie, więc po
zmianach w plikach statycznych (np. `app.js`) wystarczy `systemctl restart
taxpilot`. Jeśli statyki idą przez Cloudflare, dodatkowo wyczyść cache CDN.

### Cykliczne odświeżanie korpusu — dwa warianty

Ta sama logika (`refresh_corpus`: re-ingest aktów = najnowszy t.j. + nowele,
opcjonalnie interpretacje KIS) dostępna na dwa sposoby — wybierz jeden:

**A) Pełen Celery (zalecane przy ~16 GB).** Worker wykonuje, Beat planuje wg
`CELERY_BEAT_SCHEDULE` (pon. 04:00). Daje też ingest on-demand z admina i retry.

```bash
cp deploy/taxpilot-worker.service /etc/systemd/system/
cp deploy/taxpilot-beat.service   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now taxpilot-worker taxpilot-beat
```

**B) Timer systemd (lżejszy — bez always-on workera).** Krótko żyjący proces
ładuje embedder, odświeża i kończy. Sensowny na maszynach ~4 GB.

```bash
cp deploy/taxpilot-refresh.service /etc/systemd/system/
cp deploy/taxpilot-refresh.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now taxpilot-refresh.timer
# test ręczny: python manage.py refresh_corpus --act CIT
```

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
