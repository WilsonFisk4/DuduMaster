"""
Pogoda w Warszawie.

- TERAZ: pomiary z drogowych stacji meteorologicznych Zarządu Oczyszczania Miasta
  (API UM Warszawy, endpoint get_zom_pogoda, 16 stacji, odświeżane co ok. 10 min).
  Wartości łączymy medianą, żeby pojedyncza wadliwa stacja nie zniekształciła wyniku.
- ZA 4 GODZINY / JUTRO: prognoza z Open-Meteo.com (model pogodowy, dane bez klucza API).
"""

import logging
import math
import statistics
from datetime import datetime, timedelta

import requests

import ztm_api
from ztm_api import STREFA, ZtmApiError

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
WARSZAWA_LAT, WARSZAWA_LON = 52.2297, 21.0122
CACHE_STACJI_S = 120
CACHE_PROGNOZY_S = 900

NAZWY_DNI = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]

# kody pogody WMO używane przez Open-Meteo
KODY_POGODY = {
    0: ("☀️", "bezchmurnie"),
    1: ("🌤", "przeważnie słonecznie"),
    2: ("⛅", "częściowe zachmurzenie"),
    3: ("☁️", "pochmurno"),
    45: ("🌫", "mgła"),
    48: ("🌫", "mgła osadzająca szron"),
    51: ("🌦", "lekka mżawka"),
    53: ("🌦", "mżawka"),
    55: ("🌦", "gęsta mżawka"),
    56: ("🌧", "marznąca mżawka"),
    57: ("🌧", "gęsta marznąca mżawka"),
    61: ("🌧", "lekki deszcz"),
    63: ("🌧", "deszcz"),
    65: ("🌧", "silny deszcz"),
    66: ("🌧", "marznący deszcz"),
    67: ("🌧", "silny marznący deszcz"),
    71: ("🌨", "lekki śnieg"),
    73: ("🌨", "śnieg"),
    75: ("❄️", "silny śnieg"),
    77: ("🌨", "ziarna śniegu"),
    80: ("🌦", "przelotny deszcz"),
    81: ("🌧", "silne przelotne opady"),
    82: ("⛈", "gwałtowne opady deszczu"),
    85: ("🌨", "przelotny śnieg"),
    86: ("🌨", "silny przelotny śnieg"),
    95: ("⛈", "burza"),
    96: ("⛈", "burza z gradem"),
    99: ("⛈", "silna burza z gradem"),
}

# skąd wieje (dopełniacz), od północy co 45 stopni
KIERUNKI_WIATRU = [
    "północy", "północnego wschodu", "wschodu", "południowego wschodu",
    "południa", "południowego zachodu", "zachodu", "północnego zachodu",
]


# --------------------------------------------------------------------------- #
# Formatowanie
# --------------------------------------------------------------------------- #

def _liczba(wartosc, miejsca=1):
    """12.0 -> '12', 7.46 -> '7,5' (polski przecinek)."""
    tekst = f"{round(wartosc, miejsca):.{miejsca}f}".rstrip("0").rstrip(".")
    return (tekst if tekst not in ("-0", "") else "0").replace(".", ",")


def _stopnie(wartosc):
    return f"{_liczba(wartosc)}°C"


def _opis_pogody(kod):
    ikona, opis = KODY_POGODY.get(int(kod), ("🌡", "brak opisu"))
    return ikona, opis


def _kierunek_wiatru(stopnie):
    """Skąd wieje: 0° = z północy, 90° = ze wschodu itd."""
    nazwa = KIERUNKI_WIATRU[int(((stopnie % 360) + 22.5) // 45) % 8]
    return ("ze " if nazwa == "wschodu" else "z ") + nazwa


def _wiatr(predkosc, stopnie=None):
    if predkosc is None:
        return "brak danych"
    tekst = f"{_liczba(predkosc)} m/s"
    if stopnie is not None and predkosc >= 0.5:
        tekst += f", {_kierunek_wiatru(stopnie)}"
    return tekst


# --------------------------------------------------------------------------- #
# TERAZ: stacje miejskie
# --------------------------------------------------------------------------- #

def _float(wartosc):
    try:
        return float(wartosc)
    except (TypeError, ValueError):
        return None


def pobierz_stacje():
    """Aktualne odczyty ze wszystkich stacji (cache 2 min)."""

    def producent():
        dane = ztm_api._post("get_zom_pogoda")
        if not isinstance(dane, list):
            raise ZtmApiError("Nieoczekiwana odpowiedź API pogody")
        stacje = []
        for s in dane:
            if not isinstance(s, dict):
                continue
            temp = _float(s.get("temp_pow"))
            if temp is None or not -50 <= temp <= 60:
                continue  # wadliwy czujnik
            try:
                czas = datetime.strptime(s.get("czas"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=STREFA)
            except (ValueError, TypeError):
                czas = None
            stacje.append(
                {
                    "nazwa": s.get("lokalizacja") or "?",
                    "czas": czas,
                    "temp": temp,
                    "temp_nawierzchni": _float(s.get("temp_naw")),
                    "wilgotnosc": _float(s.get("wilg_pow")),
                    "wiatr": _float(s.get("pred_wiatru")),
                    "kierunek": _float(s.get("kier_wiatru")),
                    "opad": _float(s.get("int_opadu")),
                }
            )
        if not stacje:
            raise ZtmApiError("Brak danych ze stacji pogodowych")
        return stacje

    return ztm_api._z_cache(("stacje_pogody",), CACHE_STACJI_S, producent)


def _mediana(stacje, pole):
    wartosci = [s[pole] for s in stacje if s[pole] is not None]
    return statistics.median(wartosci) if wartosci else None


def _kierunek_sredni(stacje):
    """Średni kierunek wiatru (średnia kołowa, ważona prędkością), tylko dla stacji z wiatrem."""
    sin = cos = 0.0
    for s in stacje:
        if s["kierunek"] is not None and s["wiatr"] and s["wiatr"] >= 0.5:
            rad = math.radians(s["kierunek"])
            sin += s["wiatr"] * math.sin(rad)
            cos += s["wiatr"] * math.cos(rad)
    if sin == 0 and cos == 0:
        return None
    return math.degrees(math.atan2(sin, cos)) % 360


def tekst_teraz(teraz=None):
    stacje = pobierz_stacje()
    n = len(stacje)
    temp = _mediana(stacje, "temp")
    nawierzchnia = _mediana(stacje, "temp_nawierzchni")
    wilgotnosc = _mediana(stacje, "wilgotnosc")
    wiatr = _mediana(stacje, "wiatr")
    kierunek = _kierunek_sredni(stacje)
    z_opadem = [s for s in stacje if s["opad"] is not None and s["opad"] > 0]
    czasy = [s["czas"] for s in stacje if s["czas"]]
    teraz = teraz or datetime.now(STREFA)

    linie = [f"🌡 Pogoda w Warszawie teraz", ""]
    linie.append(f"Temperatura: {_stopnie(temp)}")
    if nawierzchnia is not None:
        linie.append(f"Temperatura nawierzchni: {_stopnie(nawierzchnia)}")
    if wilgotnosc is not None:
        linie.append(f"Wilgotność: {_liczba(wilgotnosc, 0)}%")
    linie.append(f"Wiatr: {_wiatr(wiatr, kierunek)}")
    if z_opadem:
        linie.append(f"🌧 Opady: na {len(z_opadem)} z {n} stacji")
    else:
        linie.append("☀️ Opady: brak")
    linie.append("")
    if czasy:
        najnowszy = max(czasy)
        linie.append(f"Pomiar z {najnowszy:%H:%M} (mediana z {n} stacji miejskich)")
        if teraz - najnowszy > timedelta(minutes=60):
            linie.append("⚠️ Dane mogą być nieaktualne - stacje dawno się nie odświeżały.")
    else:
        linie.append(f"Mediana z {n} stacji miejskich")
    return "\n".join(linie)


# --------------------------------------------------------------------------- #
# ZA 4 GODZINY / JUTRO: prognoza Open-Meteo
# --------------------------------------------------------------------------- #

def pobierz_prognoze():
    """Prognoza godzinowa i dzienna dla Warszawy (cache 15 min)."""

    def producent():
        parametry = {
            "latitude": WARSZAWA_LAT,
            "longitude": WARSZAWA_LON,
            "hourly": ",".join(
                [
                    "temperature_2m", "apparent_temperature", "precipitation_probability",
                    "precipitation", "weather_code", "wind_speed_10m", "wind_gusts_10m",
                    "wind_direction_10m",
                ]
            ),
            "daily": ",".join(
                [
                    "weather_code", "temperature_2m_max", "temperature_2m_min",
                    "precipitation_sum", "precipitation_probability_max", "wind_speed_10m_max",
                ]
            ),
            "wind_speed_unit": "ms",
            "timezone": "Europe/Warsaw",
            "forecast_days": 3,
        }
        try:
            odpowiedz = ztm_api._pobierz_sesje().get(OPEN_METEO_URL, params=parametry, timeout=(5, 20))
            odpowiedz.raise_for_status()
            dane = odpowiedz.json()
            hourly, daily = dane["hourly"], dane["daily"]
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.warning("Błąd pobierania prognozy Open-Meteo: %s", e)
            raise ZtmApiError("Nie udało się pobrać prognozy") from e
        godziny = {}
        for i, czas in enumerate(hourly["time"]):
            godziny[czas] = {k: hourly[k][i] for k in hourly if k != "time"}
        dni = {}
        for i, dzien in enumerate(daily["time"]):
            dni[dzien] = {k: daily[k][i] for k in daily if k != "time"}
        return {"godziny": godziny, "dni": dni}

    return ztm_api._z_cache(("prognoza_om",), CACHE_PROGNOZY_S, producent)


def tekst_za_godzin(godzin=4, teraz=None):
    teraz = teraz or datetime.now(STREFA)
    prognoza = pobierz_prognoze()
    cel = (teraz + timedelta(hours=godzin, minutes=30)).replace(minute=0, second=0, microsecond=0)
    g = prognoza["godziny"].get(cel.strftime("%Y-%m-%dT%H:00"))
    if g is None:
        raise ZtmApiError("Brak prognozy na wybraną godzinę")
    ikona, opis = _opis_pogody(g["weather_code"])
    linie = [
        f"🕓 Prognoza na {cel:%H:%M} (za ok. {godzin} godz.)",
        "",
        f"{ikona} {opis.capitalize()}",
        f"Temperatura: {_stopnie(g['temperature_2m'])} "
        f"(odczuwalna {_stopnie(g['apparent_temperature'])})",
        f"Wiatr: {_wiatr(g['wind_speed_10m'], g['wind_direction_10m'])}, "
        f"porywy do {_liczba(g['wind_gusts_10m'])} m/s",
    ]
    prawdop = g["precipitation_probability"]
    if g["precipitation"] and g["precipitation"] > 0:
        linie.append(f"🌧 Opady: {_liczba(g['precipitation'])} mm (prawdop. {prawdop}%)")
    else:
        linie.append(f"Opady: brak (prawdop. {prawdop}%)" if prawdop else "Opady: brak")
    return "\n".join(linie)


def tekst_jutro(teraz=None):
    teraz = teraz or datetime.now(STREFA)
    jutro = teraz + timedelta(days=1)
    klucz = jutro.strftime("%Y-%m-%d")
    prognoza = pobierz_prognoze()
    d = prognoza["dni"].get(klucz)
    if d is None:
        raise ZtmApiError("Brak prognozy na jutro")
    ikona, opis = _opis_pogody(d["weather_code"])
    linie = [
        f"📅 Prognoza na jutro ({NAZWY_DNI[jutro.weekday()]}, {jutro:%d.%m})",
        "",
        f"{ikona} {opis.capitalize()}",
        f"Temperatura: od {_stopnie(d['temperature_2m_min'])} do {_stopnie(d['temperature_2m_max'])}",
    ]
    if d["precipitation_sum"] and d["precipitation_sum"] > 0:
        linie.append(
            f"🌧 Opady: {_liczba(d['precipitation_sum'])} mm "
            f"(prawdop. do {d['precipitation_probability_max']}%)"
        )
    else:
        linie.append(f"Opady: brak (prawdop. do {d['precipitation_probability_max'] or 0}%)")
    linie.append(f"Wiatr: do {_liczba(d['wind_speed_10m_max'])} m/s")

    pory = []
    for godzina, nazwa in ((8, "rano"), (14, "po południu"), (20, "wieczorem")):
        g = prognoza["godziny"].get(f"{klucz}T{godzina:02d}:00")
        if g:
            ik, _ = _opis_pogody(g["weather_code"])
            pory.append(f"{ik} {nazwa} {godzina}:00 – {_stopnie(g['temperature_2m'])}")
    if pory:
        linie += ["", *pory]
    return "\n".join(linie)
