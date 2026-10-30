"""
Lokalna baza tras linii zbudowana ze statycznego GTFS Warszawy.

Źródło: https://mkuran.pl/gtfs/warsaw.zip (dane ZTM Warszawa przetworzone do GTFS,
atrybucja: ZTM Warszawa, Mikołaj Kuranowski, OpenStreetMap contributors).

Plik jest pobierany i przetwarzany RAZ NA DOBĘ do SQLite (data/trasy.db).
Dzięki temu po podaniu numeru linii bot nie robi żadnych zapytań do internetu:
krańcówki i lista przystanków w kolejności są czytane z lokalnej bazy.
"""

import csv
import io
import logging
import os
import re
import sqlite3
import threading
import time
import zipfile
import zlib
from datetime import timedelta
from collections import Counter, defaultdict

import requests

logger = logging.getLogger(__name__)

GTFS_URL = "https://mkuran.pl/gtfs/warsaw.zip"
KATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SCIEZKA_ZIP = os.path.join(KATALOG, "warsaw.zip")
SCIEZKA_DB = os.path.join(KATALOG, "trasy.db")

MAKS_WIEK_BAZY_S = 24 * 3600
# Kierunek (krańcówka) pokazujemy, jeśli ma co najmniej tyle kursów co ćwierć
# najpopularniejszego - odsiewa zjazdy do zajezdni i kursy techniczne.
PROG_UDZIALU = 0.25

# Zwiększ, gdy zmienia się struktura bazy - bot sam ją wtedy przebuduje.
WERSJA_SCHEMATU = 3

ROUTE_TYPE_METRO = "1"
# stop_id przystanków autobusowo-tramwajowych: 4 cyfry zespołu + 2 cyfry słupka (np. 100101);
# peronów metra: 4 cyfry + 'M:P' + numer (np. 6059M:P1)
_WZORZEC_STOP_BUS = re.compile(r"^\d{6}$")
_WZORZEC_STOP_METRO = re.compile(r"^\d{4}M:P\d+$")

_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Budowanie bazy
# --------------------------------------------------------------------------- #

def _pobierz_zip():
    os.makedirs(KATALOG, exist_ok=True)
    tmp = SCIEZKA_ZIP + ".tmp"
    ostatni_blad = None
    for proba in range(1, 4):
        try:
            with requests.get(GTFS_URL, stream=True, timeout=(10, 60)) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for kawalek in r.iter_content(chunk_size=1024 * 1024):
                        f.write(kawalek)
            os.replace(tmp, SCIEZKA_ZIP)
            return
        except requests.RequestException as e:
            ostatni_blad = e
            logger.warning("Pobieranie GTFS, próba %d nieudana: %s", proba, e)
            time.sleep(5 * proba)
    raise RuntimeError(f"Nie udało się pobrać GTFS: {ostatni_blad}")


def _na_sekundy(czas):
    """'05:23:00' -> sekundy od północy (godziny mogą przekraczać 24)."""
    h, m, s = czas.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _csv(zf, nazwa):
    return csv.DictReader(io.TextIOWrapper(zf.open(nazwa), encoding="utf-8-sig", newline=""))


def _zbuduj_baze():
    logger.info("Buduję lokalną bazę tras z GTFS...")
    start = time.time()
    tmp_db = SCIEZKA_DB + ".tmp"
    if os.path.exists(tmp_db):
        os.remove(tmp_db)

    with zipfile.ZipFile(SCIEZKA_ZIP) as zf:
        # 1. linie
        nazwy_linii = {}
        typy_tras = {}
        for r in _csv(zf, "routes.txt"):
            nazwy_linii[r["route_id"]] = (r["route_short_name"] or r["route_id"]).strip().upper()
            typy_tras[r["route_id"]] = r["route_type"]

        # 2. przystanki: autobusowo-tramwajowe (zespół + słupek) i perony metra
        przystanki = {}
        for r in _csv(zf, "stops.txt"):
            sid = r["stop_id"]
            if _WZORZEC_STOP_BUS.match(sid):
                przystanki[sid] = (r["stop_name"], sid[4:], r["stop_lat"], r["stop_lon"])
            elif _WZORZEC_STOP_METRO.match(sid):
                przystanki[sid] = (r["stop_name"], "", r["stop_lat"], r["stop_lon"])

        # 3. kursy -> warianty tras
        licznik = defaultdict(Counter)   # (linia, kierunek, nagłówek) -> {wariant: liczba kursów}
        pierwszy_kurs = {}               # (linia, kierunek, nagłówek, wariant) -> trip_id
        kursy_metra = {}                 # trip_id -> (linia, kierunek, nagłówek, service_id)
        for t in _csv(zf, "trips.txt"):
            naglowek = (t["trip_headsign"] or "").strip()
            if not naglowek:
                continue
            linia = t["route_id"]
            if typy_tras.get(linia) == ROUTE_TYPE_METRO:
                kursy_metra[t["trip_id"]] = (linia, t["direction_id"], naglowek, t["service_id"])
            klucz = (linia, t["direction_id"], naglowek)
            licznik[klucz][t["variant_code"]] += 1
            pierwszy_kurs.setdefault(klucz + (t["variant_code"],), t["trip_id"])

        # 4. wybór krańcówek dla każdej linii
        wg_linii = defaultdict(list)
        for (linia, kierunek, naglowek), warianty in licznik.items():
            wg_linii[linia].append((sum(warianty.values()), kierunek, naglowek, warianty))

        wybrane = []  # (route_id, kierunek, naglowek, wariant, liczba_kursow, trip_id)
        for linia, grupy in wg_linii.items():
            maks = max(g[0] for g in grupy)
            najlepsze_na_kierunek = {}
            for g in grupy:
                if g[1] not in najlepsze_na_kierunek or g[0] > najlepsze_na_kierunek[g[1]][0]:
                    najlepsze_na_kierunek[g[1]] = g
            for g in sorted(grupy, key=lambda x: -x[0]):
                if g[0] >= PROG_UDZIALU * maks or g is najlepsze_na_kierunek[g[1]]:
                    wariant = g[3].most_common(1)[0][0]
                    trip_id = pierwszy_kurs[(linia, g[1], g[2], wariant)]
                    wybrane.append((linia, g[1], g[2], wariant, g[0], trip_id))

        # 5. przebieg tras: jednokrotne przejście po stop_times (tylko wybrane kursy)
        potrzebne = {w[5].encode("utf-8"): w[5] for w in wybrane}
        potrzebne.update({t.encode("utf-8"): t for t in kursy_metra})  # metro: wszystkie rodzaje dni
        sekwencje = defaultdict(list)  # trip_id -> [(lp, stop_id, czas_przyjazdu_s)]
        with zf.open("stop_times.txt") as plik:
            plik.readline()  # nagłówek
            for surowa in plik:
                tid = surowa[: surowa.index(b",")]
                if tid in potrzebne:
                    _, lp, stop_id, przyjazd, _ = surowa.decode("utf-8").split(",", 4)
                    sekwencje[potrzebne[tid]].append((int(lp), stop_id, _na_sekundy(przyjazd)))

        # 5b. metro kursuje w odstępach (frequencies.txt), a nie wg stałych godzin
        czestotliwosci = []
        for r in _csv(zf, "frequencies.txt"):
            if r["trip_id"] in kursy_metra:
                czestotliwosci.append(
                    (r["trip_id"], _na_sekundy(r["start_time"]), _na_sekundy(r["end_time"]),
                     int(r["headway_secs"]))
                )
        uslugi_metra = {v[3] for v in kursy_metra.values()}
        dni_metra = []  # daty z wprost wskazanym rozkładem metra (np. święta -> rozkład niedzielny)
        for r in _csv(zf, "calendar_dates.txt"):
            if r["exception_type"] == "1" and r["service_id"] in uslugi_metra:
                dni_metra.append((r["date"], r["service_id"]))

    # 6. zapis do SQLite
    con = sqlite3.connect(tmp_db)
    try:
        con.executescript(
            """
            CREATE TABLE kierunki (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kod INTEGER NOT NULL,
                linia TEXT NOT NULL,
                kierunek_gtfs TEXT,
                naglowek TEXT NOT NULL,
                wariant TEXT,
                liczba_kursow INTEGER,
                metro INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX idx_kierunki_linia ON kierunki(linia);
            CREATE INDEX idx_kierunki_kod ON kierunki(kod);
            CREATE TABLE przebieg (
                kierunek_id INTEGER NOT NULL,
                lp INTEGER NOT NULL,
                stop_id TEXT NOT NULL,
                PRIMARY KEY (kierunek_id, lp)
            );
            CREATE TABLE przystanki (
                stop_id TEXT PRIMARY KEY,
                nazwa TEXT NOT NULL,
                slupek TEXT NOT NULL,
                lat REAL,
                lon REAL
            );
            CREATE TABLE meta (klucz TEXT PRIMARY KEY, wartosc TEXT);
            CREATE TABLE metro_kursy (
                trip_id TEXT PRIMARY KEY,
                kod INTEGER NOT NULL,
                service_id TEXT NOT NULL
            );
            CREATE INDEX idx_metro_kursy_kod ON metro_kursy(kod);
            CREATE TABLE metro_czestotliwosc (
                trip_id TEXT NOT NULL,
                start_s INTEGER NOT NULL,
                end_s INTEGER NOT NULL,
                odstep_s INTEGER NOT NULL
            );
            CREATE INDEX idx_metro_cz_trip ON metro_czestotliwosc(trip_id);
            CREATE TABLE metro_offsety (
                trip_id TEXT NOT NULL,
                stop_id TEXT NOT NULL,
                offset_s INTEGER NOT NULL,
                PRIMARY KEY (trip_id, stop_id)
            );
            CREATE TABLE metro_dni (data TEXT NOT NULL, service_id TEXT NOT NULL);
            """
        )
        con.executemany(
            "INSERT INTO przystanki VALUES (?,?,?,?,?)",
            [(sid, n, s, float(la), float(lo)) for sid, (n, s, la, lo) in przystanki.items()],
        )
        for linia, kierunek, naglowek, wariant, liczba, trip_id in sorted(
            wybrane, key=lambda w: (nazwy_linii.get(w[0], w[0]), -w[4])
        ):
            nazwa_linii = nazwy_linii.get(linia, linia.upper())
            # stabilny kod kierunku - przyciski w starych wiadomościach czatu
            # nadal działają po codziennej przebudowie bazy
            kod = zlib.crc32(f"{nazwa_linii}|{kierunek}|{naglowek}".encode("utf-8"))
            kursor = con.execute(
                "INSERT INTO kierunki "
                "(kod, linia, kierunek_gtfs, naglowek, wariant, liczba_kursow, metro) "
                "VALUES (?,?,?,?,?,?,?)",
                (kod, nazwa_linii, kierunek, naglowek, wariant, liczba,
                 1 if typy_tras.get(linia) == ROUTE_TYPE_METRO else 0),
            )
            kid = kursor.lastrowid
            kolejne = [
                (kid, i, stop_id)
                for i, (_, stop_id, _) in enumerate(sorted(sekwencje.get(trip_id, [])), start=1)
                if stop_id in przystanki
            ]
            con.executemany("INSERT INTO przebieg VALUES (?,?,?)", kolejne)

        # metro: kursy, odstępy między pociągami i czasy przejazdu do kolejnych stacji
        for trip_id, (linia, kierunek, naglowek, service_id) in kursy_metra.items():
            kod = zlib.crc32(
                f"{nazwy_linii.get(linia, linia.upper())}|{kierunek}|{naglowek}".encode("utf-8")
            )
            con.execute("INSERT INTO metro_kursy VALUES (?,?,?)", (trip_id, kod, service_id))
            postoje = sorted(sekwencje.get(trip_id, []))
            if postoje:
                start_kursu = postoje[0][2]
                con.executemany(
                    "INSERT OR REPLACE INTO metro_offsety VALUES (?,?,?)",
                    [(trip_id, sid, czas - start_kursu) for _, sid, czas in postoje],
                )
        con.executemany("INSERT INTO metro_czestotliwosc VALUES (?,?,?,?)", czestotliwosci)
        con.executemany("INSERT INTO metro_dni VALUES (?,?)", dni_metra)
        con.execute("INSERT INTO meta VALUES ('zbudowano', ?)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
        con.execute("INSERT INTO meta VALUES ('wersja', ?)", (str(WERSJA_SCHEMATU),))
        con.commit()
    finally:
        con.close()

    os.replace(tmp_db, SCIEZKA_DB)  # atomowa podmiana - bot działa cały czas
    logger.info("Baza tras gotowa (%.0f s, %d wariantów kierunków).", time.time() - start, len(wybrane))


# --------------------------------------------------------------------------- #
# Aktualizacja
# --------------------------------------------------------------------------- #

def _wersja_bazy():
    try:
        con = sqlite3.connect(SCIEZKA_DB)
        try:
            wiersz = con.execute("SELECT wartosc FROM meta WHERE klucz = 'wersja'").fetchone()
        finally:
            con.close()
        return int(wiersz[0]) if wiersz else None
    except (sqlite3.Error, ValueError):
        return None


def baza_gotowa():
    """Baza istnieje i ma aktualną strukturę (starsza wersja zostanie przebudowana)."""
    return os.path.exists(SCIEZKA_DB) and _wersja_bazy() == WERSJA_SCHEMATU


def baza_aktualna():
    return baza_gotowa() and (time.time() - os.path.getmtime(SCIEZKA_DB)) < MAKS_WIEK_BAZY_S


def aktualizuj(force=False):
    """Pobiera GTFS i przebudowuje bazę, jeśli jest starsza niż doba. Zwraca True, gdy odświeżono."""
    with _lock:
        if not force and baza_aktualna():
            return False
        try:
            _pobierz_zip()
            _zbuduj_baze()
            return True
        finally:
            # plik zip (ok. 100 MB) nie jest już potrzebny
            if os.path.exists(SCIEZKA_ZIP):
                os.remove(SCIEZKA_ZIP)


# --------------------------------------------------------------------------- #
# Zapytania do lokalnej bazy
# --------------------------------------------------------------------------- #

def _polacz():
    con = sqlite3.connect(SCIEZKA_DB)
    con.row_factory = sqlite3.Row
    return con


def pobierz_kierunki(linia):
    """Krańcówki (kierunki) danej linii, od najpopularniejszej."""
    con = _polacz()
    try:
        rows = con.execute(
            "SELECT kod, linia, naglowek, wariant, liczba_kursow, metro FROM kierunki "
            "WHERE linia = ? ORDER BY kierunek_gtfs, liczba_kursow DESC",
            (str(linia).strip().upper(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def pobierz_kierunek(kod):
    """Pojedynczy kierunek po stabilnym kodzie (albo None)."""
    con = _polacz()
    try:
        r = con.execute(
            "SELECT kod, linia, naglowek, wariant, metro FROM kierunki WHERE kod = ? LIMIT 1", (kod,)
        ).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def pobierz_przebieg(kod):
    """
    Przystanki trasy w kolejności: [{'lp','stop_id','zespol','slupek','nazwa'}, ...].
    Dla metra 'slupek' jest pusty, a 'stop_id' to np. '6059M:P1'.
    """
    con = _polacz()
    try:
        rows = con.execute(
            "SELECT p.lp, p.stop_id, s.nazwa, s.slupek FROM przebieg p "
            "JOIN przystanki s ON s.stop_id = p.stop_id "
            "WHERE p.kierunek_id = (SELECT id FROM kierunki WHERE kod = ? LIMIT 1) "
            "ORDER BY p.lp",
            (kod,),
        ).fetchall()
        return [
            {
                "lp": i,
                "stop_id": r["stop_id"],
                "zespol": r["stop_id"][:4],
                "slupek": r["slupek"],
                "nazwa": r["nazwa"],
            }
            for i, r in enumerate(rows, start=1)
        ]
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Metro: odjazdy z rozkładu (API ZTM nie udostępnia ich dla metra)
# --------------------------------------------------------------------------- #

_TYP_DNIA = {0: "Pc", 1: "Pc", 2: "Pc", 3: "Pc", 4: "Pt", 5: "Sb", 6: "Nd"}  # pon..niedz


def _prefiks_dnia(con, dzien):
    """
    Rodzaj rozkładu metra na dany dzień: Pc (pon-czw), Pt (piątek), Sb (sobota), Nd (niedziela/święta).
    Dni świąteczne są wprost wskazane w kalendarzu GTFS, pozostałe wynikają z dnia tygodnia.
    """
    wiersz = con.execute(
        "SELECT service_id FROM metro_dni WHERE data = ? LIMIT 1", (dzien.strftime("%Y%m%d"),)
    ).fetchone()
    if wiersz:
        return wiersz["service_id"][:2]
    return _TYP_DNIA[dzien.weekday()]


def odjazdy_metra(kod, stop_id, teraz, ile=6):
    """
    Najbliższe odjazdy metra ze stacji (peronu) w danym kierunku.
    Zwraca (lista_napisow, odstep_w_minutach_lub_None). Metro jeździ w odstępach,
    więc godziny są orientacyjne (wyliczone z początku kursu + czasu przejazdu).
    """
    con = _polacz()
    try:
        sek_teraz = teraz.hour * 3600 + teraz.minute * 60 + teraz.second
        odjazdy = set()
        odstep_teraz = None
        for przesuniecie in (-1, 0, 1):  # wczorajszy dzień (kursy po północy), dziś, jutro
            dzien = teraz.date() + timedelta(days=przesuniecie)
            prefiks = _prefiks_dnia(con, dzien)
            wiersze = con.execute(
                "SELECT k.trip_id, o.offset_s FROM metro_kursy k "
                "JOIN metro_offsety o ON o.trip_id = k.trip_id AND o.stop_id = ? "
                "WHERE k.kod = ? AND k.service_id LIKE ?",
                (stop_id, kod, prefiks + "%"),
            ).fetchall()
            for w in wiersze:
                okna = con.execute(
                    "SELECT start_s, end_s, odstep_s FROM metro_czestotliwosc WHERE trip_id = ?",
                    (w["trip_id"],),
                ).fetchall()
                for okno in okna:
                    t = okno["start_s"]
                    while t < okno["end_s"]:
                        odjazdy.add(t + w["offset_s"] + przesuniecie * 86400)
                        t += okno["odstep_s"]
                    if (
                        przesuniecie == 0
                        and okno["start_s"] <= sek_teraz < okno["end_s"]
                        and odstep_teraz is None
                    ):
                        odstep_teraz = round(okno["odstep_s"] / 60, 1)

        kolejne = sorted(t for t in odjazdy if t >= sek_teraz)[:ile]
        napisy = []
        for t in kolejne:
            napis = f"{(t % 86400) // 3600:02d}:{(t % 3600) // 60:02d}"
            if t >= 86400:
                napis += " (po północy)"
            napisy.append(napis)
        return napisy, odstep_teraz
    finally:
        con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    aktualizuj(force=True)
