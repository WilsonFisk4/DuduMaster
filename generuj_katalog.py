"""
Generuje plik katalog_przystankow.csv - listę WSZYSTKICH przystanków w Warszawie
(nazwa, ulica, współrzędne, linie i kierunki), z której wybierasz swoje miejsca do miejsca.py.

Uruchomienie:  python generuj_katalog.py
Plik otwórz w Excelu (albo LibreOffice / Arkuszach Google) i szukaj Ctrl+F po nazwie lub ulicy.
"""

import csv
import io
import os
import re
import zipfile
from collections import Counter, defaultdict

KATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SCIEZKA_ZIP = os.path.join(KATALOG, "warsaw.zip")
WYJSCIE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "katalog_przystankow.csv")
GTFS_URL = "https://mkuran.pl/gtfs/warsaw.zip"

WZORZEC_BUS = re.compile(r"^\d{6}$")
WZORZEC_METRO = re.compile(r"^\d{4}M:P\d+$")
KIERUNKOW_NA_LINIE = 2  # ile najpopularniejszych kierunków pokazać przy każdej linii


def pobierz_zip():
    import requests  # tylko tutaj, żeby skrypt działał też z gotowym plikiem zip

    os.makedirs(KATALOG, exist_ok=True)
    print("Pobieram GTFS (ok. 100 MB)...")
    with requests.get(GTFS_URL, stream=True, timeout=(10, 60)) as r:
        r.raise_for_status()
        with open(SCIEZKA_ZIP, "wb") as f:
            for kawalek in r.iter_content(chunk_size=1024 * 1024):
                f.write(kawalek)


def csv_z_zip(zf, nazwa):
    return csv.DictReader(io.TextIOWrapper(zf.open(nazwa), encoding="utf-8-sig", newline=""))


def klucz_sortowania(wiersz):
    return (wiersz["nazwa"].casefold(), wiersz["id"])


def main():
    usun_po_sobie = False
    if not os.path.exists(SCIEZKA_ZIP):
        pobierz_zip()
        usun_po_sobie = True

    with zipfile.ZipFile(SCIEZKA_ZIP) as zf:
        linie = {r["route_id"]: (r["route_short_name"] or r["route_id"]).strip() for r in csv_z_zip(zf, "routes.txt")}

        przystanki = {}
        for r in csv_z_zip(zf, "stops.txt"):
            sid = r["stop_id"]
            if WZORZEC_BUS.match(sid) or WZORZEC_METRO.match(sid):
                przystanki[sid] = r

        kursy = {}  # trip_id -> (linia, kierunek)
        for t in csv_z_zip(zf, "trips.txt"):
            if t["trip_headsign"].strip():
                kursy[t["trip_id"].encode("utf-8")] = (
                    linie.get(t["route_id"], t["route_id"]),
                    t["trip_headsign"].strip(),
                )

        print("Analizuję rozkłady (ok. pół minuty)...")
        na_przystanku = defaultdict(Counter)  # stop_id -> {(linia, kierunek): liczba kursów}
        przystanki_b = {s.encode("utf-8"): s for s in przystanki}
        with zf.open("stop_times.txt") as plik:
            plik.readline()
            for surowa in plik:
                tid, _, reszta = surowa.partition(b",")
                kurs = kursy.get(tid)
                if kurs is None:
                    continue
                stop_b = reszta.split(b",", 2)[1]
                if stop_b in przystanki_b:
                    na_przystanku[przystanki_b[stop_b]][kurs] += 1

    wiersze = []
    for sid, r in przystanki.items():
        wg_linii = defaultdict(list)
        for (linia, kierunek), liczba in na_przystanku.get(sid, {}).items():
            wg_linii[linia].append((liczba, kierunek))

        def klucz_linii(l):
            return (not l[0].isdigit(), int(l[0]) if l[0].isdigit() else 0, l[0])

        opisy = []
        for linia, kierunki in sorted(wg_linii.items(), key=klucz_linii):
            najlepsze = [k for _, k in sorted(kierunki, reverse=True)[:KIERUNKOW_NA_LINIE]]
            opisy.append(f"{linia} → {' / '.join(najlepsze)}")

        metro = bool(WZORZEC_METRO.match(sid))
        wiersze.append(
            {
                "nazwa": r["stop_name"],
                "ulica": r.get("street_name", ""),
                "id": sid,
                "rodzaj": "metro" if metro else "bus/tram",
                "linie": "; ".join(opisy),
                "lat": r["stop_lat"],
                "lon": r["stop_lon"],
            }
        )

    wiersze.sort(key=klucz_sortowania)
    with open(WYJSCIE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Przystanek", "Ulica", "ID (do miejsca.py)", "Rodzaj", "Linie i kierunki", "Szerokość", "Długość"])
        for x in wiersze:
            w.writerow([x["nazwa"], x["ulica"], x["id"], x["rodzaj"], x["linie"], x["lat"], x["lon"]])

    print(f"Gotowe: {len(wiersze)} przystanków zapisano w {WYJSCIE}")
    if usun_po_sobie and os.path.exists(SCIEZKA_ZIP):
        os.remove(SCIEZKA_ZIP)


if __name__ == "__main__":
    main()
