"""
Modele TaxPilot — PostgreSQL jako system of record.

OpenSearch jest indeksem wyszukiwania; tu trzymamy prawdę: zarejestrowane
akty, chunki (z id w OpenSearch), historię zadań ingestu, oceny kwalifikacji
i historię czatu.
"""

from __future__ import annotations

from django.db import models

# Wartości spójne z core config.ULGI / source_type
ULGA_CHOICES = [("BR", "Ulga B+R"), ("IPBOX", "IP Box"), ("PKUP", "Koszty autorskie"), ("", "—")]
SOURCE_CHOICES = [
    ("ustawa", "Ustawa"),
    ("objasnienia", "Objaśnienia MF"),
    ("interpretacja", "Interpretacja"),
    ("orzeczenie", "Orzeczenie"),
]


class Akt(models.Model):
    """Zarejestrowany akt prawny (wpis z core config.ACTS + stan ingestu)."""

    kod = models.CharField(max_length=16, unique=True)  # CIT / PIT / ORD
    title = models.CharField(max_length=512)
    publisher = models.CharField(max_length=16, default="DU")
    year = models.IntegerField()
    position = models.IntegerField()
    citation_suffix = models.CharField(max_length=128)
    eli_id = models.CharField(max_length=64, blank=True)
    last_ingested_at = models.DateTimeField(null=True, blank=True)
    # Nowelizacje uchwalone po tekście jednolitym (sygnał aktualności stanu prawnego).
    nowele_po_tj = models.IntegerField(default=0)
    nowele = models.JSONField(default=list, blank=True)  # [{eli,date,title}] — newest first

    class Meta:
        ordering = ["kod"]
        verbose_name = "Akt"
        verbose_name_plural = "Akty"

    def __str__(self) -> str:
        return f"{self.kod} — {self.title}"


class Chunk(models.Model):
    """Jednostka redakcyjna zaindeksowana w OpenSearch (kopia-prawda w Postgres)."""

    akt = models.ForeignKey(Akt, on_delete=models.CASCADE, related_name="chunks")
    opensearch_id = models.CharField(max_length=64, unique=True)
    article_num = models.CharField(max_length=16)
    ustep = models.CharField(max_length=16, blank=True)
    citation = models.CharField(max_length=256)
    ulga = models.CharField(max_length=16, choices=ULGA_CHOICES, blank=True)
    source_type = models.CharField(max_length=24, choices=SOURCE_CHOICES, default="ustawa")
    content_text = models.TextField()
    content_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    eli_id = models.CharField(max_length=64, blank=True)
    obowiazuje_od = models.DateField(null=True, blank=True)
    obowiazuje_do = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["akt", "article_num", "ustep"]
        indexes = [
            models.Index(fields=["ulga"]),
            models.Index(fields=["source_type"]),
            models.Index(fields=["article_num"]),
        ]

    def __str__(self) -> str:
        return self.citation


class IngestJob(models.Model):
    """Przebieg ingestu (uruchamiany przez Celery lub management command)."""

    STATUS = [
        ("pending", "Oczekuje"),
        ("running", "W toku"),
        ("success", "Sukces"),
        ("failed", "Błąd"),
    ]

    akt = models.ForeignKey(Akt, on_delete=models.SET_NULL, null=True, related_name="jobs")
    status = models.CharField(max_length=16, choices=STATUS, default="pending")
    obowiazuje_od = models.DateField(null=True, blank=True)
    chunks_indexed = models.IntegerField(default=0)
    error = models.TextField(blank=True)
    celery_task_id = models.CharField(max_length=64, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        akt = self.akt.kod if self.akt else "?"
        return f"Ingest {akt} [{self.status}]"


class QualificationQuery(models.Model):
    """Historia ocen asystenta kwalifikacji."""

    opis = models.TextField()
    ulgi = models.JSONField(default=list)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "Qualification queries"

    def __str__(self) -> str:
        return f"Kwalifikacja {self.created_at:%Y-%m-%d %H:%M}"


class ChatSession(models.Model):
    session_key = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Sesja {self.session_key[:8]}"


class ChatMessage(models.Model):
    ROLE = [("user", "user"), ("assistant", "assistant")]

    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=16, choices=ROLE)
    content = models.TextField()
    sources = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.role}: {self.content[:40]}"
