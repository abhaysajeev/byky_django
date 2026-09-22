from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render


def healthz(request):
    """Liveness plus a database round trip, for Docker's health check."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok", "database": "ok"})


def home(request):
    return render(request, "home.html")
