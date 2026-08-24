# TaxPilot — asystent ulg podatkowych (RAG)

**Demo:** taxpilot.cytr.us

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
robi to cyklicznie — orkiestrator rozsyła osobne zadania per akt (izolacja
błędów), a ingest jest inkrementalny: liczy embeddingi tylko dla chunków,
których treść realnie się zmieniła (hash SHA-256).

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
| `search.py` | hybryda z filtrami (ulga, akt, stan prawny), `retrieve_mixed` (kwota per typ źródła), `detect_ulga` (auto-zawężenie z treści pytania) + generacja |
| `qualification.py` | asystent kwalifikacji (B+R / IP Box / 50% KUP) |
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
| `ulgi/tasks.py` | zadania Celery: `ingest_act_task`, `ingest_interpretacje_task`, `refresh_corpus_task` (orkiestrator per akt) |
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

**Secure-by-default:** `DJANGO_DEBUG` jest domyślnie wyłączony (produkcja). Do
pracy lokalnej ustaw w `.env`:

```bash
DJANGO_DEBUG=1
DJANGO_SECRET_KEY=dowolny-losowy-klucz-do-dev
```

W produkcji (`DJANGO_DEBUG=0`) aplikacja **wymaga** ustawienia własnego
`DJANGO_SECRET_KEY` (inaczej start jest przerywany) i automatycznie włącza
hardening (Secure cookies, HSTS, nosniff). SSL-redirect jest opcjonalny
(`DJANGO_SECURE_SSL_REDIRECT=1`) — zwykle robi to już Cloudflare/nginx.

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

python manage.py runserver           # dev (wymaga DJANGO_DEBUG=1)
# produkcyjnie: gunicorn taxpilot_site.wsgi  (patrz deploy/)
```

Health-check (liveness + readiness bazy) dla nginx/systemd/monitoringu:
`GET /healthz` → `200 {"status": "ok"}` lub `503` przy niedostępnej bazie.

## Jakość kodu i testy

Lint, format i testy konfiguruje `pyproject.toml`. Zależności deweloperskie:

```bash
pip install -r requirements-dev.txt   # ruff + pytest(-django) + mypy + django-stubs + narzędzia dev

ruff check .            # lint
ruff format --check .    # weryfikacja formatu (bez `--check` formatuje)
mypy .                   # typy (django-stubs rozumie modele/manager `.objects`)
pytest                   # testy jednostkowe (czysta logika, bez sieci/DB)
```

`mypy` używa wtyczki `django-stubs` (`django_settings_module` w `pyproject.toml`),
więc ładuje `settings` przy starcie — potrzebuje `DJANGO_DEBUG=1` i
`DJANGO_SECRET_KEY` w `.env` (lub ENV). CI ustawia te zmienne samo.

Przy `DJANGO_DEBUG=1` — i tylko gdy pakiety dev są zainstalowane — wpinają
się automatycznie: django-debug-toolbar (panel SQL/czasów, skonfigurowany
pod HTMX/kwalifikację) oraz django-extensions (`shell_plus`,
`runserver_plus`; ten drugi wymaga Werkzeug). Na produkcji (`DEBUG=0`) nic
z tego się nie ładuje.

Testy (`tests/`) pokrywają logikę niezależną od usług zewnętrznych: klucze
semantycznego cache, budowanie filtrów/zapytań OpenSearch, chunking aktów,
logikę retrievalu (kwoty per typ źródła w `retrieve_mixed`, auto-detekcja
ulgi), helpery widoków i budowanie promptu. CI (`.github/workflows/ci.yml`)
uruchamia lint + format + mypy + testy na każdym push/PR.

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

W wariancie Celery `refresh_corpus_task` to orkiestrator: rozsyła osobne
zadania per akt i per ulgę (limit czasu per zadanie: soft 1 h 45 / hard 2 h),
więc timeout jednego aktu nie przewraca pozostałych. Dzięki inkrementalnemu
ingestowi akt bez zmian kończy się w sekundy (`policzono=0`); pełne
przeliczenie wymusza `--force` (np. po zmianie modelu embeddera).

Worker Celery przydaje się też do ingestu on-demand (`ingest_act_task.delay()`)
i automatycznych retry — patrz `deploy/` po jednostki systemd.

### Monitoring kolejki (Flower)

Opcjonalny panel webowy do podglądu workerów i zadań (statusy, czasy, retry):

```bash
pip install flower
celery -A taxpilot_site flower --address=127.0.0.1 --port=5555 --basic_auth=admin:haslo
```

Domyślnie tylko na `127.0.0.1` — wchodź tunelem SSH (`-L 5555:127.0.0.1:5555`) albo
przez nginx z basic-auth. Jednostka systemd i konfiguracja nginx w `deploy/`
(`taxpilot-flower.service`, `nginx-taxpilot-flower.conf`).

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
zamiast trzymać worker. Ingest jest idempotentny (`_id = doc_id`) i
inkrementalny (hash treści chunku — ponowny przebieg bez zmian nie liczy
żadnych embeddingów), więc można go bezpiecznie ponawiać; `--force` wymusza
pełne przeliczenie.

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
- [x] Fundament jakości: `ruff` (lint+format), `mypy` + `django-stubs`, testy `pytest`, CI (GitHub Actions), hardening produkcyjny `settings.py`, endpoint `/healthz`.
- [x] Retrieval z kwotą per typ źródła (`retrieve_mixed`) i automatyczną detekcją ulgi z pytania (`detect_ulga`); kwalifikacja rozszerzona o 50% KUP.
- [x] Inkrementalny ingest po hashu treści (embedding tylko zmian, `--force`) + odświeżanie rozbite na zadania per akt (orkiestrator, limity czasu na zadanie).
