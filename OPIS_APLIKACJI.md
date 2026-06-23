# TaxPilot — Szczegółowy opis działania aplikacji

## Spis treści

1. [Cel i kontekst](#1-cel-i-kontekst)
2. [Architektura ogólna](#2-architektura-ogólna)
3. [Baza wiedzy i indeksowanie](#3-baza-wiedzy-i-indeksowanie)
4. [Trzy źródła i ich pozyskiwanie](#4-trzy-źródła-i-ich-pozyskiwanie)
5. [Przepływ danych — od pytania do odpowiedzi](#5-przepływ-danych--od-pytania-do-odpowiedzi)
6. [Moduł wyszukiwania (search.py)](#6-moduł-wyszukiwania-searchpy)
7. [Hybryda BM25 + kNN](#7-hybryda-bm25--knn)
8. [Generacja odpowiedzi i budowanie kontekstu](#8-generacja-odpowiedzi-i-budowanie-kontekstu)
9. [Cache semantyczny (Redis)](#9-cache-semantyczny-redis)
10. [Asystent kwalifikacji](#10-asystent-kwalifikacji)
11. [Backend (Django) i frontend (HTMX)](#11-backend-django-i-frontend-htmx)
12. [Modele danych i konfiguracja](#12-modele-danych-i-konfiguracja)
13. [Narzędzia (komendy zarządzające)](#13-narzędzia-komendy-zarządzające)
14. [Kluczowe decyzje projektowe](#14-kluczowe-decyzje-projektowe)

---

## 1. Cel i kontekst

### Co to jest RAG?

Aplikacja jest systemem **RAG (Retrieval-Augmented Generation)** — „generowanie wspomagane wyszukiwaniem". Modele językowe mają dwie wady: ich wiedza jest **zamrożona w czasie** (nie znają zmian w prawie po dacie treningu) i potrafią **zmyślać** (halucynacje). W prawie podatkowym oba błędy są kosztowne — przestarzała stawka albo wymyślony przepis to realne ryzyko dla podatnika.

RAG rozwiązuje to tak, że **najpierw wyszukuje właściwe fragmenty** z własnej, kontrolowanej bazy przepisów, a dopiero potem podaje je modelowi jako materiał źródłowy. Model odpowiada wyłącznie na podstawie tego, co dostał — jak doradca, który cytuje konkretny artykuł ustawy, a nie ogólne wrażenie o przepisach.

### Problem, który rozwiązuje

Trzy najpopularniejsze ulgi dla firm technologicznych i twórczych — **B+R**, **IP Box** i **50% koszty autorskie** — są rozproszone po ustawach o PIT i CIT, objaśnieniach Ministra Finansów i tysiącach interpretacji indywidualnych KIS. Ocena, czy konkretna działalność się kwalifikuje, wymaga zestawienia przepisu, jego urzędowej wykładni i linii interpretacyjnej. Tradycyjne wyszukiwanie pełnotekstowe szuka dokładnych słów — pytanie „czy programiście przysługują podwyższone koszty" nie trafi do przepisu mówiącego o „honorarium autorskim z tytułu rozporządzania prawami autorskimi", choć semantycznie to to samo.

TaxPilot łączy wyszukiwanie leksykalne (BM25 z polskim stemmerem — precyzyjne terminy), wyszukiwanie semantyczne (kNN — rozumie sens) i syntezę przez LLM (czytelna odpowiedź z cytatem do konkretnego artykułu i rozwijanym fragmentem źródła).

### Trzy źródła wiedzy

- **Ustawy (akty ELI)** — ustawa o PIT, ustawa o CIT i Ordynacja podatkowa, pobierane jako **najnowszy tekst jednolity** z [eli.gov.pl](https://eli.gov.pl) (API ELI).
- **Objaśnienia podatkowe MF** — urzędowa wykładnia (objaśnienia IP Box z 15.07.2019, interpretacja ogólna 50% KUP z 15.09.2020), PDF-y z gov.pl.
- **Interpretacje indywidualne KIS** — z systemu EUREKA (`eureka.mf.gov.pl`), publiczne API bez logowania.

### Trzy ulgi (kotwice przepisów)

| Ulga | Kod | Przepisy kotwiczące |
|------|-----|---------------------|
| Badawczo-rozwojowa | `BR` | art. 18d CIT · art. 26e PIT |
| IP Box (5% od kwalifikowanego IP) | `IPBOX` | art. 24d/24e CIT · art. 30ca/30cb PIT |
| 50% koszty autorskie | `PKUP` | art. 22 ust. 9 pkt 3 PIT |

---

## 2. Architektura ogólna

Aplikacja to monolit **Django + HTMX** (server-side rendering ze strumieniowaniem SSE), z PostgreSQL jako systemem zapisu i OpenSearch jako warstwą wyszukiwania:

```
┌─────────────────────────────────────────────────────┐
│                   nginx :44306                       │
│   /            → Django (gunicorn 127.0.0.1:8503)    │
│   /static/*    → pliki statyczne (collectstatic)     │
│   taxpilot.cytr.us / uodo-rag.cytr.us (Cloudflare)   │
└───────────────────────┬──────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────┐
│                    Django (ulgi/)                     │
│   views.chat        ← strona, pieczęć stanu prawnego  │
│   views.ask         ← streaming RAG po SSE            │
│   views.qualify     ← ocena kwalifikacji (HTMX)       │
└───┬──────────────┬───────────────┬───────────────────┘
    │              │               │
    ▼              ▼               ▼
 search.py     qualification.py  ulgi/cache.py
 (retrieve,    (assess)          (Redis: exact +
  hybryda)                        semantyczny)
    │              │
    ▼              ▼
 OpenSearch 3.x          Ollama Cloud
 (kNN + BM25 + RRF)      (deepseek-v4-flash:cloud,
 Docker :9200            streaming po SSE)
    │
    ▼
 PostgreSQL  ←── system zapisu (Akt, Chunk, IngestJob,
                 QualificationQuery, ChatSession/Message)
```

**Zewnętrzne zależności:**

- **PostgreSQL** — system zapisu (*system of record*). Trzyma akty, chunki, historię ingestów i rozmów. OpenSearch można w każdej chwili odtworzyć z Postgresa przez ponowny ingest.
- **OpenSearch 3.x** — wektorowa baza z wbudowanym BM25. Przechowuje embeddingi (dim=1024) i tekst chunków. Kontener Docker, dostępny lokalnie (`127.0.0.1:9200`). Indeks `taxpilot`.
- **SentenceTransformers** — embeddingi modelem `sdadas/stella-pl-retrieval-mini-8k` (dim=1024), uruchamianym **w procesie** aplikacji (GPU, fallback CPU).
- **Ollama Cloud** — generacja odpowiedzi modelem `deepseek-v4-flash:cloud`; połączenie przez klucz API w nagłówku `Authorization`.
- **Redis** — podwójna rola: broker zadań Celery (ingest on-demand i cykliczne odświeżanie korpusu) oraz cache odpowiedzi (warstwa exact + semantyczna).
- **Django + HTMX** — backend renderujący HTML; HTMX obsługuje wymianę fragmentów (kwalifikacja) i SSE (streaming odpowiedzi). Bez budowania frontu (zero Node po stronie serwera).

---

## 3. Baza wiedzy i indeksowanie

### 3.1 Co to jest embedding i dlaczego jest potrzebny?

Komputery nie rozumieją tekstu — operują na liczbach. **Embedding** zamienia tekst na wektor liczb tak, że teksty o podobnym znaczeniu mają podobne wektory. Zdania „przeniesienie autorskich praw majątkowych do programu" i „honorarium za rozporządzanie prawami autorskimi" są napisane innymi słowami, ale model embeddingowy umieszcza je blisko siebie; „przepis na bigos" trafia daleko.

Model `stella-pl-retrieval-mini-8k` wymaga **różnych prefiksów** dla zapytań i dokumentów:

- Dokumenty indeksowane są **bez prefiksu**.
- Zapytania embedowane są **z prefiksem** `"Instruct: Given a web search query, retrieve relevant passages that answer the query.\nQuery: "`.

Embedder (`embedder.py`) ładowany jest leniwie jako singleton. Na GPU pracuje w fp16; na obu urządzeniach wymuszamy standardową atencję PyTorcha zamiast `xformers` (model wywołuje `xformers.ops.fmha` bezpośrednio, a na CPU oraz na nowych GPU bez zgodnego wheela `xformers.ops` bywa `None`).

### 3.2 Granularność indeksowania — artykuł / ustęp

Akty są indeksowane **na poziomie artykułu lub ustępu**, nie całego aktu. Każdy chunk to osobny dokument w OpenSearch z własnym embeddingiem i prefiksem kontekstu `[art. N ...]`. Logika w `chunking.py`:

- artykuł bez ustępów → jeden chunk,
- artykuł z ustępami → chunk per ustęp,
- fragment dłuższy niż `CHUNK_MAX_CHARS = 1200` → **twardy podział** (`_split_long`: linie → zdania → cięcie po znakach), tak by nie został ucięty przez limit 512 tokenów embeddera. Każda część zachowuje prefiks `[art. N ...]`.

Dla źródeł prozą (objaśnienia, interpretacje) działa generyczny `chunk_document()` — ten sam podział długich fragmentów, ale bez struktury ustępów.

Identyfikator dokumentu (`doc_id`) jest deterministyczny:

```
{eli_id}:art{article_num}:u{ustep|0}:c{chunk_index}   →   hash
```

Determinizm `doc_id` daje **idempotentny ingest**: ponowne zaindeksowanie tego samego aktu nadpisuje dokumenty (`_id = doc_id`) zamiast tworzyć duplikaty.

### 3.3 Struktura indeksu OpenSearch

Wszystkie chunki trafiają do jednego indeksu `taxpilot`. Kluczowe pola:

| Pole | Typ | Opis |
|------|-----|------|
| `embedding` | knn_vector | wektor dim=1024, HNSW, silnik **lucene**, `space_type: cosinesimil` |
| `content_text` | text (`standard`) | tekst chunku; zachowuje dosłowne tokeny (sygnatury, „Dz. U.") |
| `content_text.pl` | text (`polish_custom`) | podpole z polskim stemmerem (multi-field) |
| `citation` | keyword/text | np. „art. 18d ust. 1 ustawy o CIT" lub sygnatura interpretacji |
| `ulga` | keyword | `BR` / `IPBOX` / `PKUP` (do filtrowania) |
| `source_type` | keyword | `ustawa` / `objasnienia` / `interpretacja` |
| `eli_id` | keyword | ELI aktu (puste dla objaśnień/interpretacji) |
| `zrodlo_url` | keyword | link do źródła (ELI / PDF MF / podgląd EUREKA) |
| `article_num`, `ustep`, `obowiazuje_od` | — | metadane do filtrów i wyświetlania |

#### Polski analizator (morfologik / Stempel)

Indeks definiuje custom analyzer `polish_custom` (lowercase → polish_stop → polish_stem), gdzie `polish_stem` to **lematyzacja słownikowa** (plugin `analysis-morfologik`) lub algorytmiczna (`analysis-stempel`). Bez stemmera BM25 traktowałby „kwalifikowane" i „kwalifikowanych" jako różne tokeny.

Multi-field daje dwa pola jednocześnie: `content_text` (oryginalne tokeny) i `content_text.pl` (lematy). Zapytania BM25 używają obu, z większą wagą podpola `.pl` (łapie fleksję), a pole bazowe pozostaje **fallbackiem** dla tokenów, które stemmer mógłby zniekształcić (sygnatury, oznaczenia jak „Dz. U.").

### 3.4 PostgreSQL jako system zapisu

Każdy chunk istnieje równolegle w Postgresie (model `Chunk`) i w OpenSearchu. Postgres jest źródłem prawdy: ingest robi `Chunk.objects.filter(akt=akt).delete()` przed ponownym zapisem (reindex „na czysto") i `bulk_create`, a OpenSearch dostaje `bulk` z `_id = doc_id`. Dzięki temu cały indeks można odtworzyć z Postgresa, a ponowny przebieg jest bezpieczny.

---

## 4. Trzy źródła i ich pozyskiwanie

### 4.1 Ustawy — samoaktualizujący się ingest ELI

`discovery.py` pyta API ELI o akty powiązane z pinowanymi ustawami (CIT/PIT/ORD) i kategoryzuje wyniki:

- **jednolity** — Obwieszczenie = **tekst jednolity** (gotowa wersja skonsolidowana; każdy t.j. jest obwieszczeniem),
- **zmieniający** — nowelizacja („o zmianie…"), której diff jest już złożony w tekście jednolitym — **nie do ingestu**,
- **bazowy** — pierwotny akt.

Resolver wybiera **najnowszy tekst jednolity** i to jego treść trafia do ingestu (`eli_client`). Dodatkowo wykrywa **nowelizacje uchwalone po dacie t.j.** — jeśli istnieją, znaczy to, że stan prawny może nie obejmować najnowszych zmian. Liczba i lista takich nowel zapisywane są na akcie (`Akt.nowele_po_tj`, `Akt.nowele`) i renderowane jako ostrzegawczy badge „⚠ nowelizacje po t.j. (N)".

### 4.2 Objaśnienia podatkowe MF

Kuratorska lista PDF-ów (`config.OBJASNIENIA`) z gov.pl. `ingest_objasnienia` pobiera PDF, ekstrahuje tekst (`pdf_to_text`), tnie `chunk_document()` i indeksuje jak pseudo-akt (patrz 4.4). Link do PDF wędruje do `zrodlo_url`.

### 4.3 Interpretacje indywidualne KIS (EUREKA)

`ulgi/kis_client.py` to klient publicznego API EUREKA (bez logowania), zrekonstruowany z ruchu sieciowego portalu:

- **Pobranie:** `GET /api/public/v1/informacje/{id}` → JSON; treść i metadane w `dokument.fields` (klucze `SYG`, `TRESC_INTERESARIUSZ`, `TEZA`, `DT_WYD`, `STATUS_INFORMACJI`, `KATEGORIA_INFORMACJI`).
- **Wyszukiwanie:** `POST /api/public/v1/wyszukiwarka/informacje/` z body `{"filter": {...}, "columns": [...]}` i paginacją. Filtry serwerowe:

  ```json
  {"PRZEPISY": [węzły przepisów],
   "KATEGORIA_INFORMACJI": [1],     // 1 = Interpretacja indywidualna
   "STATUS_INFORMACJI": [27],       // 27 = Aktualna
   "DT_WYD_start": "2023-01-01"}
  ```

  Węzły `PRZEPISY` to identyfikatory ze słownika przepisów (drzewo Ustawa → Dział → Rozdział → artykuł → ustęp → punkt). Mapa `PRZEPISY_BY_ULGA` przypina je do naszych kotwic, np. PKUP celuje w węzeł **art. 22 ust. 9 pkt 3** (precyzyjny przepis o 50% kosztach), a nie w całe art. 22.

Klient korzysta z sesji `requests` z ponawianiem (backoff na timeouty i 5xx/429), bo EUREKA bywa wolna.

### 4.4 Pseudo-akty — bez migracji schematu

Objaśnienia i interpretacje nie są aktami ELI, ale reużywają tych samych modeli `Akt`/`Chunk`. Rejestrowane są jako **pseudo-akt** z pustym `eli_id` (`_register_pseudo_akt`), co pozwala wpiąć je w istniejący klucz obcy `Chunk.akt` **bez żadnej zmiany schematu**. Pieczęć „stan prawny" i badge nowel filtrują akty z niepustym `eli_id`, więc dotyczą tylko realnych ustaw.

---

## 5. Przepływ danych — od pytania do odpowiedzi

```
Użytkownik wpisuje pytanie (lub klika przykład / wybiera filtr ulgi)
         │
         ▼
[app.js: TaxPilot.ask()] → POST /ask (q, ulga)
         │
         ▼
[views.ask] → services.answer()
   │
   ├── cache.get(q, filtry, model)
   │      ├── exact (hash) → natychmiastowy hit
   │      └── semantyczny (kosinus ≥ 0.95) → hit
   │   (przy trafieniu: SSE token + sources, koniec)
   │
   ├── retrieve(q, ulga, …)  ← hybryda BM25+kNN (search.py)
   │      → docs (z content_text, zrodlo_url)
   │
   ├── _build_context(docs)  ← ponumerowane podstawy prawne
   │
   └── stream Ollama (SSE):
          event: token   → dopisywany w bąbelku na żywo
          event: sources → źródła z linkiem i rozwijanym fragmentem
          event: done
   (po zakończeniu: zapis ChatMessage + cache.set)
```

Strumieniowanie idzie po **SSE** (`StreamingHttpResponse`, `text/event-stream`). Frontend (`app.js`) parsuje zdarzenia i dopisuje tokeny do bąbelka, a na końcu renderuje sekcję „Źródła".

---

## 6. Moduł wyszukiwania (search.py)

`retrieve()` to serce retrievalu:

```python
def retrieve(query, *, k=TOP_K, akt=None, ulga=None,
             source_types=None, on_date=None, use_hybrid=True):
    vector  = embed_query(query)                 # embedding z prefiksem instrukcji
    filters = build_filters(akt, ulga, source_types, on_date)
    body    = hybrid_body(query, vector, k, filters, use_hybrid)
    # hybryda → search_pipeline = taxpilot-hybrid-pipeline
    resp    = client.search(index=OPENSEARCH_INDEX, body=body, params=...)
    return hits_to_docs(resp["hits"]["hits"])     # _source → docs (+ _score)
```

Cechy:

- **Filtry osadzone w pod-queries** (`ulga`, `source_type`, `obowiazuje_od ≤ on_date`), nie jako `post_filter` — `top_k` liczone jest już po filtrowaniu.
- **Fallback do czystego kNN** — jeśli zapytanie hybrydowe się wywali (np. brak pipeline), `retrieve` ponawia bez hybrydy, logując ostrzeżenie. Strona nigdy nie zostaje bez wyników z powodu chwilowego problemu pipeline.
- `hits_to_docs` kopiuje całe `_source` do dokumentu (więc `content_text` i `zrodlo_url` są dostępne w warstwie widoku — stąd fragmenty i linki przy źródłach).

---

## 7. Hybryda BM25 + kNN

`hybrid_body()` składa dwa zapytania w klauzulę `hybrid` z pipeline `taxpilot-hybrid-pipeline`:

```
query.hybrid.queries = [
  { bool: { must: [multi_match na content_text + content_text.pl], filter: [...] } },   # BM25
  { knn:  { embedding: { vector, k, filter: [...] } } }                                  # kNN
]
```

**Pipeline RRF** (`normalization-processor`):

- Normalizacja: `min_max` (osobno dla BM25 i kNN, do skali [0, 1]),
- Kombinacja: `arithmetic_mean` z wagami `[BM25 = 0.6, kNN = 0.4]`.

Pipeline aktualizowany jest **idempotentnie** (PUT) przy każdym ingeście — zmiana wag w kodzie propaguje się do klastra bez ręcznej interwencji.

**Dlaczego BM25 ma większą wagę?** W prawie podatkowym zapytania zawierają precyzyjne terminy (*„wskaźnik nexus"*, *„honorarium autorskie"*, *„koszty kwalifikowane"*), które embeddingi potrafią rozmyć w stronę pojęć ogólnych. Polski stemmer po stronie BM25 już daje elastyczność fleksyjną, którą normalnie musiałby zapewnić kNN — stąd przesunięcie wagi ku leksyce.

---

## 8. Generacja odpowiedzi i budowanie kontekstu

**Budowanie kontekstu** (`_build_context`) zestawia pobrane dokumenty w ponumerowane bloki „podstaw prawnych" (cytat + treść chunku), które trafiają do promptu użytkownika. Model dostaje wyłącznie ten materiał.

**Prompt systemowy** (`_SYSTEM`) instruuje model, by odpowiadał **tylko na podstawie podanego kontekstu**, cytował konkretne artykuły i nie zmyślał, gdy materiału brak. To kluczowy element anti-halucynacji: gdy w bazie nie ma odpowiedzi, model ma to powiedzieć, a nie improwizować.

**Generacja** idzie do **Ollama Cloud** (`deepseek-v4-flash:cloud`) endpointem `/api/chat` ze `stream: true`. Tokeny są retransmitowane do przeglądarki jako zdarzenia SSE `token`. Embedding zapytania liczony jest lokalnie (lekki, pojedynczy), generacja w chmurze (ciężka) — VPS nie musi hostować dużego LLM.

---

## 9. Cache semantyczny (Redis)

`ulgi/cache.py` (`SemanticResponseCache`) ma dwie warstwy:

1. **exact** — hash znormalizowanego pytania + filtry + model → natychmiastowy hit. Współdzielony przez Redis (`redis://…/2`), więc działa między workerami gunicorna.
2. **semantyczny** — kosinus wektora pytania względem zapamiętanych wektorów; hit, gdy podobieństwo ≥ `SEM_THRESHOLD = 0.95`. Wektory liczy ten sam model co wyszukiwanie, więc cache „rozumie" parafrazy.

**Dlaczego próg aż 0.95?** W prawie podatkowym lepiej policzyć odpowiedź raz za dużo niż podać użytkownikowi cache dla pytania tylko *pozornie* podobnego (np. „ulga B+R w CIT" vs „ulga B+R w PIT" — semantycznie blisko, prawnie różnie). Wysoki próg minimalizuje fałszywe trafienia.

Przy trafieniu cache odpowiedź i źródła wracają natychmiast (z tagiem warstwy w UI), bez wołania LLM.

---

## 10. Asystent kwalifikacji

Drugi tryb (zakładka „Kwalifikacja") odpowiada na inne pytanie: „czy moja działalność kwalifikuje się do ulg?". Użytkownik opisuje, czym zajmuje się zespół; `qualification.assess()`:

1. dla każdej rozpatrywanej ulgi wykonuje `retrieve()` (przepisy + objaśnienia + interpretacje), deduplikuje dokumenty,
2. buduje kontekst i prosi LLM o **ustrukturyzowaną ocenę**: werdykt (kwalifikuje się / częściowo / nie / brak danych), uzasadnienie, podstawa prawna i „czego brakuje do oceny",
3. zwraca `oceny` + `sources` (z `content_text`, `zrodlo_url`, `eli_id` — by widok mógł pokazać link i rozwijany fragment).

Werdykty renderowane są jako karty z kolorami (zielony/bursztynowy/czerwony), a pod nimi „Przepisy użyte do oceny" z linkiem do źródła i fragmentem z bazy. Istotne: asystent **potrafi odmówić** — opis niespełniający przesłanek twórczości/systematyczności dostaje werdykt „nie kwalifikuje się", co dowodzi, że ocena stoi na przepisie, a nie na uprzejmości modelu.

---

## 11. Backend (Django) i frontend (HTMX)

### 11.1 Widoki

- `views.chat` — strona główna; dokłada do kontekstu pieczęć „stan prawny" (data z ostatniego udanego ingestu realnego aktu) i badge nowel.
- `views.ask` — `StreamingHttpResponse` z generatorem SSE (token → sources → done); zapisuje `ChatMessage` i wypełnia cache.
- `views.qualify` — `@require_POST`, renderuje fragment `_qualification.html` (podmieniany HTMX-em).
- `_source_view(d)` — ujednolicony widok źródła: rozbity cytat, tag ulgi, link (`zrodlo_url` lub ELI) i **tekst fragmentu** (`content_text`).

### 11.2 Frontend — „Kancelaria"

Szablony Django + **HTMX** (zero buildu po stronie serwera). Design „Kancelaria": papierowe tło, petrol jako akcent, pieczęć stanu prawnego jako element sygnaturowy, serif do treści, mono do etykiet i cytatów. `app.js` (waniliowy JS, moduł `TaxPilot`):

- **streaming SSE** — `fetch` + `ReadableStream`, parsowanie zdarzeń, dopisywanie tokenów na żywo (migający kursor);
- **źródła** — każde z linkiem „pełny tekst ↗" i rozwijanym „fragment ▾" (treść chunku wprost z bazy, bez zależności od zewnętrznych stron);
- **trwałe podpowiedzi** — pasek przykładowych pytań (po dwa na ulgę) pod polem pytania; klik wstawia pytanie, ustawia filtr ulgi i wysyła;
- **filtr ulgi** — radio zawężające retrieval do `BR`/`IPBOX`/`PKUP`.

W trybie kwalifikacji fragmenty renderowane są natywnym `<details>` (treść jest podmieniana HTMX-em, więc nie wymaga osobnego JS).

---

## 12. Modele danych i konfiguracja

### 12.1 Modele Django (`ulgi/models.py`)

| Model | Rola |
|-------|------|
| `Akt` | akt/pseudo-akt: `kod`, `eli_id`, `citation_suffix`, `last_ingested_at`, `nowele_po_tj`, `nowele` (JSON) |
| `Chunk` | chunk: `opensearch_id`, `article_num`, `ustep`, `citation`, `ulga`, `source_type`, `content_text`, `obowiazuje_od/do` |
| `IngestJob` | historia ingestu: `status`, `chunks_indexed`, `error`, `celery_task_id`, czasy |
| `QualificationQuery` | log zapytań kwalifikacji (`opis`, `ulgi`, `result`) |
| `ChatSession` / `ChatMessage` | historia rozmów (`role`, `content`, `sources`) |

### 12.2 Konfiguracja (`config.py`)

```python
OPENSEARCH_INDEX     = "taxpilot"
EMBED_MODEL          = "sdadas/stella-pl-retrieval-mini-8k"   # dim 1024
EMBED_BATCH_SIZE     = 8     # CPU; na GPU podbijany przez env
EMBED_MAX_SEQ        = 512
DEFAULT_OLLAMA_MODEL = "deepseek-v4-flash:cloud"
TOP_K                = 8
CHUNK_MAX_CHARS      = 1200  # powyżej — twardy podział chunku
```

Plus słowniki domenowe: `ACTS` (pinowane ustawy), `ULGI` (ulgi + kotwice), `OBJASNIENIA` (kuratorskie PDF-y MF) i `PRZEPISY_BY_ULGA` (węzły przepisów EUREKA per ulga) oraz kody filtrów KIS (kategoria=1, status=27).

### 12.3 Zadania Celery (`ulgi/tasks.py`)

Dwa zadania `@shared_task` (broker Redis, postęp w `IngestJob`):

- **`ingest_act_task`** — ingest pojedynczego aktu on-demand (np. klik w panelu admina → `delay()`), z automatycznym retry.
- **`refresh_corpus_task`** — cykliczne odświeżanie całego korpusu (re-ingest aktów: najnowszy t.j. + przeliczone nowele, opcjonalnie najnowsze interpretacje KIS). Planowane przez **Celery Beat** (`CELERY_BEAT_SCHEDULE` w `settings.py`, domyślnie co tydzień).

Logika `refresh_corpus` żyje w `ingest_core.py` i jest współdzielona: to samo odświeżanie można uruchomić zadaniem Celery **albo** komendą `manage.py refresh_corpus` (pod timer systemd) — patrz rozdziały 13 i 14.

---

## 13. Narzędzia (komendy zarządzające)

### `ingest_acts`

Ingest ustaw z ELI: resolver wybiera najnowszy tekst jednolity, `chunk_act()` tnie po artykułach/ustępach (z twardym podziałem), liczy nowelizacje po t.j., zapisuje do Postgresa i indeksuje w OpenSearch. Idempotentny (`update_or_create` aktu + reindex chunków + `_id = doc_id`).

### `ingest_objasnienia`

Ingest objaśnień MF z kuratorskiej listy (`--all` lub wybrane kody). Pobiera PDF, ekstrahuje tekst, chunkuje prozą, indeksuje jako pseudo-akt z linkiem do PDF.

### `ingest_interpretacje`

Ingest interpretacji KIS z EUREKA — cztery tryby:

```bash
# wprost po ID informacji
python manage.py ingest_interpretacje --ids 604348,639490 --ulga IPBOX

# z eksportu wyszukiwarki EUREKA (CSV/XLSX)
python manage.py ingest_interpretacje --csv eksport.xlsx --ulga BR

# wyszukiwanie po ID przepisów
python manage.py ingest_interpretacje --przepisy 35573,40951 --limit 50

# najprościej — ulga sama dobiera przepisy z PRZEPISY_BY_ULGA
python manage.py ingest_interpretacje --ulga IPBOX --limit 50 --od-daty 2023-01-01
```

Tryb wyszukiwania filtruje serwerowo do kategorii „Interpretacja indywidualna" i statusu „Aktualna", opcjonalnie po dacie (`--od-daty`/`--do-daty`); `--dry-run` wypisuje trafienia bez indeksowania. Każda interpretacja jest pobierana, czyszczona z HTML, chunkowana i indeksowana z linkiem do podglądu w EUREKA.

### `refresh_corpus`

Cykliczne odświeżanie korpusu jako krótko żyjący proces (ta sama logika co `refresh_corpus_task`, bez always-on workera). Re-ingest aktów (najnowszy t.j. + nowele), opcjonalnie interpretacje KIS:

```bash
python manage.py refresh_corpus                      # wszystkie akty
python manage.py refresh_corpus --act CIT --act PIT  # wybrane
python manage.py refresh_corpus --interpretacje --interp-limit 20 --od-daty 2024-01-01
```

Odporny na błąd pojedynczej pozycji — leci dalej i raportuje podsumowanie. Pod timer systemd (patrz rozdział 14).

### `discovery.py`

Diagnostyka ELI: wypisuje kandydatów (teksty jednolite, nowelizacje, akty bazowe) dla pinowanych ustaw — pomocne przy weryfikacji, co resolver wybierze.

---

## 14. Kluczowe decyzje projektowe

### Dlaczego Django + HTMX zamiast FastAPI + React?

Cała wartość jest po stronie serwera (retrieval, RAG, ocena kwalifikacji), a UI to formularz, strumień odpowiedzi i lista źródeł. Django + HTMX daje server-side rendering, sesje, ORM i panel admina „za darmo", a HTMX obsługuje wymianę fragmentów i SSE bez budowania SPA. Zero Node po stronie serwera, mniej ruchomych części, szybsze wdrożenie — przy zachowaniu strumieniowania na żywo.

### Dlaczego PostgreSQL jako system zapisu obok OpenSearch?

OpenSearch to indeks, nie źródło prawdy. Trzymając kanoniczne dane w Postgresie (akty, chunki, historia), możemy w każdej chwili **odtworzyć cały indeks** ponownym ingestem (np. po zmianie chunkowania albo modelu embeddingowego), nie tracąc niczego. Reindex „na czysto" (`delete` chunków aktu + `bulk_create` + `bulk` z `_id=doc_id`) jest dzięki temu w pełni idempotentny.

### Dlaczego pseudo-akty zamiast osobnych modeli dla objaśnień/interpretacji?

Objaśnienia i interpretacje reużywają modeli `Akt`/`Chunk` jako pseudo-akty z pustym `eli_id`. Daje to jednolity tor indeksowania i wyszukiwania dla trzech typów źródeł **bez żadnej migracji schematu** — `source_type` rozróżnia je w filtrach, a pusty `eli_id` wyklucza je z pieczęci stanu prawnego.

### Dlaczego samoaktualizujący się tekst jednolity + badge nowel?

Ustawy podatkowe są często nowelizowane. Zamiast pinować konkretną wersję, resolver pobiera **najnowszy tekst jednolity** (gotowy konsolidat). Dodatkowo wykrywa nowelizacje uchwalone *po* dacie t.j. i ostrzega o nich badgem — użytkownik wie, że stan prawny może nie obejmować najświeższych zmian. To uczciwość, której wymaga domena prawna.

### Dlaczego embedding na GPU (5090), a serwing na VPS?

Ciężki etap to liczenie wektorów przy ingeście (atencja S², setki–tysiące chunków). Uruchamiany lokalnie na RTX 5090 w fp16 z większym batchem skraca pełny ingest z **godzin (CPU na VPS) do minut**. Zapis idzie przez tunel SSH wprost do PostgreSQL i OpenSearch na VPS. Serwing potrzebuje tylko lekkiego embeddingu pojedynczego zapytania, więc CPU VPS wystarcza. Wektory są zgodne (ten sam model, znormalizowane), więc liczone na GPU lądują w tej samej przestrzeni co zapytania liczone na CPU. To wybór **szybkości**, a nie konieczność RAM-owa — na hoście z ~16 GB ingest na CPU również przejdzie, tylko wolniej.

### Dlaczego Celery Beat *lub* timer systemd do odświeżania?

`refresh_corpus` jest wspólną funkcją, a Celery i systemd to dwie cienkie nakładki na nią. Daje to wybór zależny od zasobów: przy hoście z ~16 GB sensowny jest **pełen Celery** (worker + Beat) — bo always-on worker (druga kopia embeddera, ~1,5–2 GB) się mieści, a przy okazji dostajemy ingest on-demand z admina i retry. Na maszynach ~4 GB lepszy jest **timer systemd** uruchamiający `manage.py refresh_corpus` jako krótko żyjący proces, bez trzymania workera w pamięci. Ta sama logika, dwa tryby wdrożenia — bez duplikacji kodu.

### Dlaczego BM25 ma większą wagę w RRF niż kNN?

Domena prawno-podatkowa operuje precyzyjnymi terminami (*„wskaźnik nexus"*, *„honorarium autorskie"*), które embeddingi rozmywają w stronę pojęć ogólnych. Polski stemmer po stronie BM25 już zapewnia tolerancję fleksji, więc część pracy kNN jest zbędna. Wagi 0.6/0.4 (BM25/kNN) dają trafniejsze dopasowania do konkretnych przepisów niż domyślne 50/50.

### Dlaczego twardy podział długich chunków?

Embedder ma limit ~512 tokenów. Bez twardego podziału długie artykuły byłyby ucinane, a ich końcówka nigdy nie trafiłaby do wektora. `_split_long` tnie nadmiarowe fragmenty (linie → zdania → znaki), zachowując prefiks `[art. N ...]`, tak by każdy chunk mieścił się w limicie i niósł kontekst.

### Dlaczego próg cache semantycznego aż 0.95?

W prawie lepiej policzyć odpowiedź raz za dużo niż podać użytkownikowi zapamiętaną odpowiedź na pytanie tylko pozornie podobne. Wysoki próg eliminuje fałszywe trafienia między bliskimi, ale prawnie różnymi zapytaniami (CIT vs PIT, B+R vs IP Box).

### Dlaczego fragment źródła z bazy, a nie tylko link?

Treść chunku jest już w indeksie (`content_text`), więc pokazujemy ją wprost — rozwijany „fragment" działa dla wszystkich typów źródeł, nie zależy od dostępności zewnętrznych portali i **dowodzi**, że odpowiedź stoi na konkretnym przepisie, a nie jest zmyślona. Link „pełny tekst ↗" prowadzi do źródła (ELI / PDF MF / EUREKA) jako uzupełnienie.

### Dlaczego interpretacje KIS przez publiczne API EUREKA?

Interpretacji 50% KUP czy IP Box są tysiące i szybko się dezaktualizują. Publiczne API EUREKA pozwala wyszukać je **po węźle przepisu** (precyzyjnie, np. art. 22 ust. 9 pkt 3) z serwerowym filtrem statusu „Aktualna" i kategorii „Interpretacja indywidualna", a następnie pobrać i zaindeksować kuratorski, świeży podzbiór — zamiast ręcznego eksportu i kopiowania.
