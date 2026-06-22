# OpenSearch + polski analizator Stempel (analysis-stempel).
#
# Stempel jest oficjalnie pakowanym pluginem OpenSearch — to ten sam
# stemmer, którego używa uodo_rag (filtr `polish_stem`). Działa pewnie
# na każdej wersji OpenSearch.
#
# morfologik (lematyzacja słownikowa) NIE jest oficjalnym pluginem
# OpenSearch — to był plugin ze świata Elasticsearch. Jeśli znajdziesz
# port `analysis-morfologik` zgodny z poniższą wersją, doinstaluj go
# osobną linią `opensearch-plugin install <URL>` i ustaw
# POLISH_STEM_FILTER=morfologik_stem. Domyślnie jedziemy na Stemplu.

# 3.7.0 — aktualna linia 3.x (2.x jest już tylko utrzymaniowa).
FROM opensearchproject/opensearch:3.7.0

RUN /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-stempel

# (opcjonalnie) morfologik z portu społecznościowego — odkomentuj i podmień URL:
# RUN /usr/share/opensearch/bin/opensearch-plugin install --batch \
#     https://example.org/opensearch-analysis-morfologik-3.7.0.zip
