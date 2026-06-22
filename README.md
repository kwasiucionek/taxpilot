# TaxPilot — asystent ulg podatkowych (RAG)

RAG nad polskim prawem podatkowym, ukierunkowany na trzy ulgi:

- **Ulga B+R** — art. 18d CIT / 26e PIT
- **IP Box** — art. 24d CIT / 30ca PIT (preferencyjne 5%)
- **Koszty autorskie / 50% KUP** — art. 22 ust. 9 pkt 3 PIT

Łączy przepisy, objaśnienia MF i interpretacje KIS, odpowiada z konkretną
podstawą prawną i nie udaje wiążącej porady — wspiera doradcę.

## Architektura

| Warstwa | Rola |
|---------|------|
| **PostgreSQL** | system of record (akty, chunki, zadania ingestu, historia) |
| **OpenSearch** | indeks wyszukiwania — hybryda BM25 (Stempel) + kNN |
| **Celery + Redis** | ingest w tle (ELI → chunking → embeddingi → bazy) |
| **stella-pl-mini** | embedder (`sdadas/stella-pl-retrieval-mini-8k`) |
| **Ollama** | generacja odpowiedzi (`deepseek-v4-flash:cloud`) |
| **Django + HTMX** | aplikacja webowa (czat + asystent kwalifikacji) |

Rdzeń RAG jest frameworkowo-niezależny; Django go owija. Dostępne też
szybkie demo w Streamlit (`app.py`) oraz opcjonalne programistyczne API
(`api.py`, FastAPI).

## Struktura repo

**Rdzeń RAG (niezależny od frameworka):**

| plik | rola |
|------|------|
| `config.py` | konfiguracja, rejestr aktów (ELI), mapa ulg→artykuły |
| `eli_client.py` | pobieranie tekstów aktów z `api.sejm.gov.pl/eli` |
| `chunking.py` | podział aktu na jednostki redakcyjne (artykuł/ustęp) |
| `opensearch_schema.py` | mapping (Stempel + kNN + daty), pipeline hybrydy, zapytania |
| `embedder.py` | stella-pl-mini (patch CPU), embed query/dokument |
| `search.py` | hybryda z filtrami (ulga, akt, stan prawny) + generacja |
| `qualification.py` | asystent kwalifikacji (B+R / IP Box) |
| `ingest.py` | core CLI ingestu (tylko OpenSearch) |
| `api.py` | opcjonalne API FastAPI |

**Aplikacja Django:**

| ścieżka | rola |
|---------|------|
| `taxpilot_site/` | projekt (settings, celery, urls, wsgi/asgi) |
| `ulgi/models.py` | Akt, Chunk, IngestJob, QualificationQuery, Chat* |
| `ulgi/ingest_core.py` | ingest do Postgres + OpenSearch |
| `ulgi/tasks.py` | zadanie Celery `ingest_act_task` |
| `ulgi/management/commands/ingest_acts.py` | ingest bez Celery |
| `app.py` | alternatywne demo w Streamlit |

## Wymagania infrastruktury

- **OpenSearch** z pluginem `analysis-stempel` (patrz `deploy/`)
- **PostgreSQL** 14+
- **Redis** (broker Celery)
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

# Ingest aktów (synchronicznie, bez Celery — oszczędność RAM):
python manage.py ingest_acts --all --od 2024-01-01
# ...albo przez Celery:  python manage.py ingest_acts --act CIT --async

python manage.py runserver           # dev
# produkcyjnie: gunicorn taxpilot_site.wsgi  (patrz deploy/)
```

Worker Celery (tylko jeśli chcesz ingest w tle on-demand):

```bash
celery -A taxpilot_site worker -l info --concurrency 1
```

## Uruchomienie (Streamlit — szybkie demo)

```bash
pip install streamlit
streamlit run app.py --server.port 8503
```

## Strategia ingestu (oszczędność RAM)

Embeddingi licz lokalnie (RTX 5090) i wgrywaj do zdalnego OpenSearcha
tunelem SSH, albo odpalaj `manage.py ingest_acts` on-demand zamiast
trzymać always-on worker Celery (druga kopia stelli w RAM).

## Wdrożenie

Patrz `deploy/README-mikrus.md` — pełny stack na Mikrusie
(OpenSearch + PostgreSQL + Redis + Django za nginx).

## TODO

- [ ] W `config.ACTS` podmienić `position` na aktualny **tekst jednolity** CIT/PIT/Ordynacji.
- [ ] Widoki HTMX: czat ze streamingiem (SSE) + tryb kwalifikacji *(w toku)*.
- [ ] Ingest interpretacji KIS (eureka.mf.gov.pl) i objaśnień MF.
- [ ] Orzecznictwo NSA (CBOSA).
- [ ] Wersjonowanie czasowe (wiele wersji artykułu z rozłącznymi przedziałami dat).
