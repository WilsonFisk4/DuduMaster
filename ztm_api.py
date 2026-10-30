"""
Klient oficjalnego API Warszawy (dane.um.warszawa.pl).

Zasady:
- jedna wspólna sesja HTTP z automatycznym ponawianiem i backoffem,
- zapytania wyłącznie "na żądanie" (odjazdy) albo z krótkim cache (utrudnienia),
- funkcje są synchroniczne - w bocie wołamy je przez asyncio.to_thread().
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BASE_URL = "https://dane.um.warszawa.pl/api/action"
STREFA = ZoneInfo("Europe/Warsaw")

CACHE_UTRUDNIEN_S = 180  # utrudnienia odświeżamy najczęściej co 3 minuty
CACHE_ODJAZDOW_S = 30    # chroni przed spamem przy wielokrotnym klikaniu "Odśwież"


class ZtmApiError(Exception):
    """Błąd komunikacji z API ZTM / UM Warszawa."""


_sesja = None
_sesja_lock = threading.Lock()
_cache = {}
_cache_lock = threading.Lock()


def _pobierz_sesje():
    global _sesja
    with _sesja_lock:
        if _sesja is None:
            sesja = requests.Session()
            retry = Retry(
                total=3,
                backoff_factor=1.0,  # 0s, 2s, 4s pomiędzy próbami
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET", "POST"}),
                respect_retry_after_header=True,
            )
            sesja.mount("https://", HTTPAdapter(max_retries=retry))
            _sesja = sesja
        return _sesja


def _token():
    token = (os.getenv("API_TOKEN") or "").strip()
    if not token:
        raise ZtmApiError("Brak API_TOKEN w pliku .env")
    return token


def _post(endpoint, payload=None):
    url = f"{BASE_URL}/{endpoint}"
    try:
        odpowiedz = _pobierz_sesje().post(
            url,
            headers={"Authorization": _token()},
            json=payload,
            timeout=(5, 20),
        )
        odpowiedz.raise_for_status()
        return json.loads(odpowiedz.content.decode("utf-8-sig"))
    except (requests.RequestException, ValueError) as e:
        logger.warning("Błąd zapytania %s: %s", endpoint, e)
        raise ZtmApiError(f"API nie odpowiada ({endpoint})") from e


def _z_cache(klucz, ttl, producent):
    teraz = time.monotonic()
    with _cache_lock:
        wpis = _cache.get(klucz)
        if wpis and teraz - wpis[0] < ttl:
            return wpis[1]
    wartosc = producent()
    with _cache_lock:
        _cache[klucz] = (time.monotonic(), wartosc)
    return wartosc


def _pola(rekord):
    """Zamienia [{'key':..,'value':..}, ...] (albo {'values': [...]}) na słownik."""
    if isinstance(rekord, dict):
        rekord = rekord.get("values", [])
    return {p.get("key"): p.get("value") for p in rekord if isinstance(p, dict)}


def _czas_na_sekundy(tekst):
    """'07:15:00' -> sekundy od północy (godziny mogą przekraczać 24 dla kursów nocnych)."""
    try:
        h, m, s = (int(x) for x in tekst.split(":"))
        return h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# Odjazdy
# --------------------------------------------------------------------------- #

def pobierz_odjazdy(zespol, slupek, linia):
    """Rozkład dzisiejszych odjazdów danej linii z jednego słupka (1 małe zapytanie)."""

    def producent():
        dane = _post(
            "get_ztm_odjazdy_linii_z_przystanku",
            {"busstopId": zespol, "busstopNr": slupek, "line": str(linia)},
        )
        if not isinstance(dane, list):
            raise ZtmApiError("Nieoczekiwana odpowiedź API odjazdów")
        odjazdy = []
        for rekord in dane:
            pola = _pola(rekord)
            sek = _czas_na_sekundy(pola.get("czas"))
            if sek is None:
                continue
            odjazdy.append(
                {
                    "sek": sek,
                    "kierunek": pola.get("kierunek") or "",
                    "trasa": pola.get("trasa") or "",
                    "brygada": pola.get("brygada") or "",
                }
            )
        return odjazdy

    return _z_cache(("odjazdy", zespol, slupek, str(linia)), CACHE_ODJAZDOW_S, producent)


def _norm(tekst):
    return " ".join((tekst or "").casefold().split())


def najblizsze_odjazdy(odjazdy, kierunek=None, ile=6, teraz=None):
    """
    Zwraca listę napisów z najbliższymi odjazdami, np. ['07:15', '07:27', 'jutro 04:30'].
    Jeśli podano kierunek (nazwa krańcówki), filtruje po nim; gdy nic nie pasuje,
    pokazuje wszystkie odjazdy tej linii z przystanku.
    """
    teraz = teraz or datetime.now(STREFA)
    sek_teraz = teraz.hour * 3600 + teraz.minute * 60 + teraz.second

    wybrane = odjazdy
    if kierunek:
        dopasowane = [o for o in odjazdy if _norm(o["kierunek"]) == _norm(kierunek)]
        if dopasowane:
            wybrane = dopasowane

    wszystkie = sorted({o["sek"] for o in wybrane})
    dzis = [t for t in wszystkie if t >= sek_teraz][:ile]
    wynik = [f"{(t % 86400) // 3600:02d}:{(t % 3600) // 60:02d}" for t in dzis]

    if len(wynik) < ile:
        # brakuje odjazdów na dziś - dopełniamy początkiem rozkładu (orientacyjnie na jutro)
        for t in [x for x in wszystkie if x < 86400][: ile - len(wynik)]:
            wynik.append(f"jutro {t // 3600:02d}:{(t % 3600) // 60:02d}")
    return wynik


# --------------------------------------------------------------------------- #
# Utrudnienia drogowe
# --------------------------------------------------------------------------- #

def _parsuj_date(tekst):
    try:
        return datetime.strptime(tekst, "%Y-%m-%d %H:%M:%S").replace(tzinfo=STREFA)
    except (ValueError, TypeError):
        return None


def pobierz_utrudnienia(tylko_aktywne=True):
    """Lista utrudnień drogowych (cache 3 min), od najnowszych."""

    def producent():
        dane = _post("get_bin_utrudnienia_drogowe")
        if not isinstance(dane, list):
            raise ZtmApiError("Nieoczekiwana odpowiedź API utrudnień")
        wynik = []
        for u in dane:
            if not isinstance(u, dict):
                continue
            wynik.append(
                {
                    "id": u.get("id"),
                    "nazwa": (u.get("name") or "Utrudnienie").strip(),
                    "start": _parsuj_date(u.get("start_date")),
                    "koniec": _parsuj_date(u.get("end_date")),
                    "tresc": [t for t in (u.get("content") or []) if isinstance(t, str)],
                }
            )
        return wynik

    wszystkie = _z_cache(("utrudnienia",), CACHE_UTRUDNIEN_S, producent)
    teraz = datetime.now(STREFA)
    if tylko_aktywne:
        wszystkie = [
            u for u in wszystkie
            if (u["start"] is None or u["start"] <= teraz)
            and (u["koniec"] is None or u["koniec"] >= teraz)
        ]
    minimum = datetime.min.replace(tzinfo=STREFA)
    return sorted(wszystkie, key=lambda u: u["start"] or minimum, reverse=True)
