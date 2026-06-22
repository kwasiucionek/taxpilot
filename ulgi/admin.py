from django.contrib import admin

from .models import Akt, Chunk, ChatMessage, ChatSession, IngestJob, QualificationQuery


@admin.register(Akt)
class AktAdmin(admin.ModelAdmin):
    list_display = ("kod", "title", "year", "position", "last_ingested_at")


@admin.register(Chunk)
class ChunkAdmin(admin.ModelAdmin):
    list_display = ("citation", "akt", "ulga", "source_type")
    list_filter = ("ulga", "source_type", "akt")
    search_fields = ("citation", "content_text")


@admin.register(IngestJob)
class IngestJobAdmin(admin.ModelAdmin):
    list_display = ("akt", "status", "chunks_indexed", "started_at", "finished_at")
    list_filter = ("status",)


@admin.register(QualificationQuery)
class QualificationQueryAdmin(admin.ModelAdmin):
    list_display = ("created_at",)


admin.site.register(ChatSession)
admin.site.register(ChatMessage)
