# TaxPilot — asystent ulg podatkowych (RAG)

RAG nad polskim prawem podatkowym, ukierunkowany na trzy ulgi:

- **Ulga B+R** — art. 18d CIT / 26e PIT
- **IP Box** — art. 24d CIT / 30ca PIT (preferencyjne 5%)
- **Koszty autorskie / 50% KUP** — art. 22 ust. 9 pkt 3 PIT

Łączy przepisy, objaśnienia MF i interpretacje KIS, odpowiada z konkretną
podstawą prawną (z cytatem do artykułu i rozwijanym fragmentem źródła) i nie
udaje wiążącej porady — wspiera doradcę.

## Architektura

| Warstwa | Rola |
|---------|------|
| **PostgreSQL** | system of record (akty, chunki, zadania ingestu, historia) |
| **OpenSearch** | indeks wyszukiwania — hybryda BM25 (Stempel) + kNN, pipeline RRF |
| **Redis** | broker Celery **oraz** semantyczny cache odpowiedzi (exact + kosinus) |
| **Celery (+ Beat)** | ingest on-demand w tle oraz cykliczne odświeżanie korpusu |
| **stella-pl-mini** | embedder (`sdadas/stella-pl-retrieval-mini-8k`, dim 1024) |
| **Ollama** | generacja odpowiedzi (`deepseek-v4-flash:cloud`), streaming po SSE |
| **Django + HTMX** | aplikacja webowa (czat ze streamingiem + asystent kwalifikacji) |

Korpus **ustaw aktualizuje się sam**: resolver ELI pobiera najnowszy tekst
jednolity i wykrywa nowelizacje uchwalone po jego publikacji (ostrzeżenie o
stanie prawnym). Objaśnienia MF i interpretacje KIS dochodzą jako pseudo-akty,
bez zmiany schematu. Zadanie `refresh_corpus` (Celery Beat lub timer systemd)
robi to cyklicznie.

Rdzeń RAG jest frameworkowo-niezależny; Django go owija. Dostępne też
szybkie demo w Streamlit (`app.py`) oraz opcjonalne programistyczne API
(`api.py`, FastAPI).

## Struktura repo

**Rdzeń RAG (niezależny od frameworka):**

| plik | rola |
|------|------|
| `config.py` | konfiguracja, rejestr aktów (ELI), mapa ulg→artykuły, źródła MF, węzły przepisów EUREKA |
| `eli_client.py` | pobieranie tekstów aktów z `api.sejm.gov.pl/eli`, ekstrakcja PDF (pdfplumber) / HTML |
| `discovery.py` | resolver ELI: wybór najnowszego tekstu jednolitego, wykrywanie nowelizacji po t.j. |
| `chunking.py` | podział aktu na jednostki redakcyjne (artykuł/ustęp) + chunkowanie prozy (objaśnienia/interpretacje), twardy podział długich fragmentów |
| `opensearch_schema.py` | mapping (Stempel/morfologik + kNN + daty), pipeline hybrydy RRF, zapytania |
| `embedder.py` | stella-pl-mini (GPU/CPU, atencja PyTorch zamiast xformers), embed query/dokument |
| `search.py` | hybryda z filtrami (ulga, akt, stan prawny) + generacja |
| `qualification.py` | asystent kwalifikacji (B+R / IP Box) |
| `ingest.py` | core CLI ingestu (tylko OpenSearch) |
| `api.py` | opcjonalne API FastAPI |

**Aplikacja Django:**

| ścieżka | rola |
|---------|------|
| `taxpilot_site/` | projekt (settings, celery + `CELERY_BEAT_SCHEDULE`, urls, wsgi/asgi) |
| `ulgi/models.py` | Akt, Chunk, IngestJob, QualificationQuery, Chat* |
| `ulgi/views.py` | widoki: czat (SSE), kwalifikacja (HTMX), widok źródeł |
| `ulgi/services.py` | warstwa usług (answer/qualify) spinająca search + cache |
| `ulgi/cache.py` | semantyczny cache odpowiedzi na Redisie (exact + kosinus ≥ 0.95) |
| `ulgi/ingest_core.py` | ingest aktów ELI + `refresh_corpus` (odświeżanie całości) |
| `ulgi/ingest_docs.py` | ingest objaśnień MF i interpretacji KIS (pseudo-akty) |
| `ulgi/kis_client.py` | klient publicznego API EUREKA (wyszukiwanie + pobieranie interpretacji) |
| `ulgi/tasks.py` | zadania Celery: `ingest_act_task`, `refresh_corpus_task` |
| `ulgi/management/commands/ingest_acts.py` | ingest ustaw (ELI) |
| `ulgi/management/commands/ingest_objasnienia.py` | ingest objaśnień MF (PDF) |
| `ulgi/management/commands/ingest_interpretacje.py` | ingest interpretacji KIS (EUREKA) |
| `ulgi/management/commands/refresh_corpus.py` | odświeżenie korpusu (dla timera systemd) |
| `app.py` | alternatywne demo w Streamlit |

## Wymagania infrastruktury

- **OpenSearch** z pluginem `analysis-stempel` (patrz `deploy/`)
- **PostgreSQL** 14+
- **Redis** (broker Celery + cache)
- **Ollama** z kluczem cloud (generacja)

## Konfiguracja

```bash
cp .env.example .env   # uzupełnij OLLAMA_CLOUD_API_KEY, hasła Postgres
```

Analizator polski: domyślnie **Stempel** (`POLISH_STEM_FILTER=polish_stem`).
morfologik nie jest oficjalnym pluginem OpenSearch — patrz `deploy/Dockerfile`.

## Uruchomienie (Django — ścieżka główna)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser

# Ingest ustaw (synchronicznie):
python manage.py ingest_acts --all --od 2024-01-01
# ...albo przez Celery:  python manage.py ingest_acts --act CIT --async

# Objaśnienia MF (kuratorska lista PDF):
python manage.py ingest_objasnienia --all

# Interpretacje KIS z EUREKA (ulga sama dobiera przepisy):
python manage.py ingest_interpretacje --ulga IPBOX --limit 50 --od-daty 2023-01-01
# ...podgląd bez indeksowania:  --dry-run

python manage.py runserver           # dev
# produkcyjnie: gunicorn taxpilot_site.wsgi  (patrz deploy/)
```

### Cykliczne odświeżanie korpusu

Re-ingest aktów (najnowszy t.j. + nowele), opcjonalnie interpretacje KIS —
ta sama logika (`refresh_corpus`) dostępna dwoma drogami:

```bash
# A) Celery Beat planuje, worker wykonuje (CELERY_BEAT_SCHEDULE w settings):
celery -A taxpilot_site worker -l info
celery -A taxpilot_site beat   -l info

# B) Timer systemd uruchamia krótko żyjącą komendę (bez always-on workera):
python manage.py refresh_corpus                 # same akty
python manage.py refresh_corpus --interpretacje # + najnowsze interpretacje KIS
```

Worker Celery przydaje się też do ingestu on-demand (`ingest_act_task.delay()`)
i automatycznych retry — patrz `deploy/` po jednostki systemd.

## Uruchomienie (Streamlit — szybkie demo)

```bash
pip install streamlit
streamlit run app.py --server.port 8503
```

## Strategia ingestu

Embeddingi można liczyć **lokalnie na GPU (RTX 5090)** i wgrywać do zdalnego
OpenSearcha/Postgresa tunelem SSH — pełny ingest schodzi wtedy z godzin (CPU na
VPS) do minut. To wybór **szybkości**, nie konieczność RAM-owa: przy hoście z
~16 GB always-on worker Celery (druga kopia embeddera, ~1,5–2 GB) mieści się
spokojnie. Na maszynach ≥ 4 GB prościej puszczać `manage.py ingest_*` / timer
zamiast trzymać worker. Ingest jest idempotentny (`_id = doc_id`), więc można go
bezpiecznie ponawiać.

## Wdrożenie

Patrz `deploy/README-mikrus.md` — pełny stack na Mikrusie
(OpenSearch + PostgreSQL + Redis + Django za nginx, worker/Beat lub timer).

## TODO

- [ ] Orzecznictwo NSA (CBOSA).
- [ ] Wersjonowanie czasowe (wiele wersji artykułu z rozłącznymi przedziałami dat).

### Zrealizowane

- [x] Resolver ELI: automatyczny najnowszy tekst jednolity + wykrywanie nowelizacji po t.j. (badge stanu prawnego).
- [x] Widoki HTMX: czat ze streamingiem (SSE) + tryb kwalifikacji.
- [x] Ingest interpretacji KIS (eureka.mf.gov.pl — wyszukiwanie po przepisie) i objaśnień MF.
- [x] Hybryda BM25 + kNN (pipeline RRF), semantyczny cache na Redisie, fragmenty źródeł z bazy.
- [x] Cykliczne odświeżanie korpusu: zadanie `refresh_corpus` (Celery Beat) + wariant timer systemd.
