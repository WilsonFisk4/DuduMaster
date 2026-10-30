"""
Komunikaty o utrudnieniach w komunikacji miejskiej przypisane do linii i metra.

Źródło: feed GTFS-Realtime Alerts z https://mkuran.pl/gtfs/warsaw/alerts.pb
(komunikaty ZTM Warszawa, licencja CC0). Format protobuf dekodujemy własnym,
minimalnym parserem, więc nie potrzeba żadnych dodatkowych bibliotek.
"""

import hashlib
import logging
import re

import requests

import ztm_api
from ztm_api import ZtmApiError

logger = logging.getLogger(__name__)

ALERTS_URL = "https://mkuran.pl/gtfs/warsaw/alerts.pb"
CACHE_KOMUNIKATOW_S = 180

_WZORZEC_METRA = re.compile(r"^M\d+$")
_WZMIANKA_O_METRZE = re.compile(
    r"\bM[12]\b"
    r"|\bmetr(?:a|u|em|ze|o\s+warszawskie)\b"
    r"|\b(?:linii?|stacj\w+|stacji|pociąg\w*|kolei)\s+metra\b"
    r"|\bmetro\s+(?:kursuje|nie\s+kursuje|zamkni\w+|jedzie)\b",
    re.IGNORECASE,
)
ROUTE_TYPE_METRO = 1


# --------------------------------------------------------------------------- #
# Minimalny dekoder protobuf (wystarcza do GTFS-RT Alerts)
# --------------------------------------------------------------------------- #

def _varint(b, i):
    wynik = przesuniecie = 0
    while True:
        bajt = b[i]
        i += 1
        wynik |= (bajt & 0x7F) << przesuniecie
        przesuniecie += 7
        if not bajt & 0x80:
            return wynik, i


def _pola_protobuf(b):
    """Zwraca listę (numer_pola, wartość); wartość to int albo bytes."""
    i, wynik = 0, []
    while i < len(b):
        klucz, i = _varint(b, i)
        numer, typ = klucz >> 3, klucz & 7
        if typ == 0:
            wartosc, i = _varint(b, i)
        elif typ == 2:
            dlugosc, i = _varint(b, i)
            wartosc = b[i : i + dlugosc]
            i += dlugosc
        elif typ == 1:
            wartosc, i = b[i : i + 8], i + 8
        elif typ == 5:
            wartosc, i = b[i : i + 4], i + 4
        else:
            raise ValueError(f"Nieobsługiwany typ protobuf: {typ}")
        wynik.append((numer, wartosc))
    return wynik


def _tekst(translated_string):
    """TranslatedString -> pierwszy napis (preferujemy język polski)."""
    tlumaczenia = []
    for numer, wartosc in _pola_protobuf(translated_string):
        if numer == 1:
            pola = dict(_pola_protobuf(wartosc))
            tlumaczenia.append(
                (pola.get(2, b"").decode("utf-8", "replace"), pola.get(1, b"").decode("utf-8", "replace"))
            )
    for jezyk, tekst in tlumaczenia:
        if jezyk.startswith("pl"):
            return tekst
    return tlumaczenia[0][1] if tlumaczenia else ""


def _czysc_opis(tekst):
    """Usuwa znaczniki markdown (###, **) z opisów komunikatów."""
    linie = [re.sub(r"^[#\s*]+", "", l).replace("**", "").strip() for l in tekst.splitlines()]
    return "\n".join(l for l in linie if l)


def _parsuj_feed(dane):
    komunikaty = []
    for numer, wartosc in _pola_protobuf(dane):
        if numer != 2:  # FeedEntity
            continue
        encja = dict(_pola_protobuf(wartosc))
        alert_bajty = encja.get(5)
        if not alert_bajty or encja.get(2):  # brak alertu albo is_deleted
            continue

        naglowek = opis = ""
        linie, metro_wg_typu = [], False
        for n, w in _pola_protobuf(alert_bajty):
            if n == 5:  # informed_entity
                pola = dict(_pola_protobuf(w))
                if pola.get(2):
                    linie.append(pola[2].decode("utf-8", "replace"))
                if pola.get(3) == ROUTE_TYPE_METRO:
                    metro_wg_typu = True
            elif n == 10:
                naglowek = _tekst(w)
            elif n == 11:
                opis = _tekst(w)

        linie_norm = {normalizuj_linie(l) for l in linie}
        opis_czysty = _czysc_opis(opis)
        komunikaty.append(
            {
                # stabilny identyfikator - przyciski w starych wiadomościach nadal działają
                "id": hashlib.md5(f"{naglowek}|{opis_czysty}".encode("utf-8")).hexdigest()[:10],
                "naglowek": naglowek.strip() or "Komunikat",
                "opis": opis_czysty,
                "linie": linie,
                "linie_norm": linie_norm,
                "metro": _czy_metro(linie_norm, metro_wg_typu, f"{naglowek}\n{opis_czysty}"),
            }
        )
    return komunikaty


def _czy_metro(linie_norm, metro_wg_typu, tekst):
    """
    Komunikat dotyczy metra, gdy:
    1. wskazuje linię M1/M2 albo typ trasy "metro", albo
    2. NIE wskazuje żadnej konkretnej linii (np. tylko przewoźnika lub przystanki),
       a w treści jest wyraźna wzmianka o metrze.
    Warunek 2 nie obejmuje komunikatów o przystankach autobusowych typu
    "METRO POLITECHNIKA 12", bo te zawsze mają przypisane linie autobusowe.
    """
    if metro_wg_typu or any(_WZORZEC_METRA.match(l) for l in linie_norm):
        return True
    if linie_norm:
        return False
    return bool(_WZMIANKA_O_METRZE.search(tekst))


# --------------------------------------------------------------------------- #
# Interfejs
# --------------------------------------------------------------------------- #

def normalizuj_linie(linia):
    """'l-4', 'L 4' -> 'L4' (w feedzie linie bywają zapisane z myślnikiem)."""
    return re.sub(r"[\s\-]", "", str(linia)).upper()


def pobierz_komunikaty():
    """Wszystkie aktualne komunikaty (cache 3 min)."""

    def producent():
        try:
            odpowiedz = ztm_api._pobierz_sesje().get(ALERTS_URL, timeout=(5, 20))
            odpowiedz.raise_for_status()
            return _parsuj_feed(odpowiedz.content)
        except (requests.RequestException, ValueError, IndexError) as e:
            logger.warning("Błąd pobierania komunikatów: %s", e)
            raise ZtmApiError("Nie udało się pobrać komunikatów") from e

    return ztm_api._z_cache(("komunikaty",), CACHE_KOMUNIKATOW_S, producent)


def znajdz_komunikat(id_komunikatu):
    return next((k for k in pobierz_komunikaty() if k["id"] == id_komunikatu), None)


def komunikaty_metra():
    return [k for k in pobierz_komunikaty() if k["metro"]]


def komunikaty_linii(linia):
    szukana = normalizuj_linie(linia)
    return [k for k in pobierz_komunikaty() if not k["metro"] and szukana in k["linie_norm"]]
