from django.urls import path

from . import views

app_name = "ulgi"
urlpatterns = [
    path("", views.chat, name="chat"),
    path("ask/", views.ask, name="ask"),
    path("kwalifikacja/", views.qualify, name="qualify"),
    path("healthz/", views.healthz, name="healthz"),
]
