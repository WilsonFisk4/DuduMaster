import asyncio
import json
import logging
import os
import re
import unicodedata
from datetime import datetime

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

import gtfs_store  # noqa: E402  (musi być po load_dotenv)
import komunikaty  # noqa: E402
import pogoda  # noqa: E402
import ztm_api  # noqa: E402
from users import DOZWOLONE_ID  # noqa: E402

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

PRZYSTANKOW_NA_STRONE = 10
MAKS_UTRUDNIEN = 10
ILE_ODJAZDOW = 6

STAN_LINIA_ODJAZD = "czekam_na_linie_odjazd"
STAN_PRZYSTANEK = "czekam_na_przystanek"
STAN_LINIA_KOMUNIKATY = "czekam_na_linie_awaria"

# Zasady historii czatu:
# - WYNIKI (odjazdy, szczegóły komunikatów, nowe wyszukiwania) wysyłamy jako NOWE wiadomości,
#   więc poprzednie sprawdzenia zostają w czacie.
# - Nawigację w obrębie jednego ekranu (strony listy, "Odśwież") edytujemy w miejscu.
# - Dane przycisków są samowystarczalne (kod kierunku, numer przystanku, id komunikatu),
#   dzięki czemu przyciski w starszych wiadomościach nadal działają.


# --------------------------------------------------------------------------- #
# Pomocnicze
# --------------------------------------------------------------------------- #

def czy_ma_dostep(user_id):
    return user_id in DOZWOLONE_ID


def normalizuj(tekst):
    """Małe litery bez polskich znaków - do wyszukiwania przystanków po nazwie."""
    tekst = tekst.casefold().replace("ł", "l")
    tekst = unicodedata.normalize("NFKD", tekst)
    return "".join(c for c in tekst if not unicodedata.combining(c)).strip()


def przycisk(tekst, dane):
    return InlineKeyboardButton(tekst, callback_data=dane)


def wiersz_menu(*dodatkowe):
    return [*dodatkowe, przycisk("🏠 Menu", "menu_start")]


def etykieta(przystanek):
    """Nazwa przystanku z numerem słupka (dla stacji metra bez słupka - sama nazwa)."""
    return f"{przystanek['nazwa']} {przystanek['slupek']}".strip()


def skroc(tekst, limit):
    return tekst if len(tekst) <= limit else tekst[: limit - 1] + "…"


async def edytuj(query, tekst, markup):
    """edit_message_text, który ignoruje błąd 'Message is not modified'."""
    try:
        await query.edit_message_text(text=tekst, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def wyslij(update, tekst, markup, nowa=True):
    """nowa=True: nowa wiadomość (zostaje w historii); nowa=False: edycja klikniętej wiadomości."""
    query = update.callback_query
    if query and not nowa:
        await edytuj(query, tekst, markup)
    elif query:
        await query.message.reply_text(tekst, reply_markup=markup)
    else:
        await update.message.reply_text(tekst, reply_markup=markup)


def format_daty(data):
    return data.strftime("%d.%m.%Y %H:%M") if data else "?"


# --------------------------------------------------------------------------- #
# Menu
# --------------------------------------------------------------------------- #

async def pokaz_menu(update, context, nowa=True):
    context.user_data.clear()
    klawiatura = InlineKeyboardMarkup(
        [
            [przycisk("🚏 O której odjazd?", "menu_odjazd")],
            [przycisk("🚨 Utrudnienia", "menu_awarie")],
            [przycisk("🌤 Pogoda", "menu_pogoda")],
        ]
    )
    await wyslij(update, "🏠 Menu", klawiatura, nowa)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not czy_ma_dostep(update.effective_user.id):
        await update.message.reply_text("⛔ Brak dostępu. Ten bot jest prywatny.")
        return
    await pokaz_menu(update, context)


# --------------------------------------------------------------------------- #
# Odjazdy: linia -> kierunek -> przystanek -> godziny
# --------------------------------------------------------------------------- #

async def pokaz_prompt_odjazd(update, context, nowa):
    if not gtfs_store.baza_gotowa():
        await wyslij(
            update,
            "⏳ Przygotowuję rozkłady (pierwsze uruchomienie, ok. minuty). Spróbuj za chwilę.",
            InlineKeyboardMarkup([wiersz_menu(przycisk("🔄 Spróbuj ponownie", "menu_odjazd"))]),
            nowa=False if update.callback_query else True,
        )
        return
    context.user_data.clear()
    context.user_data["stan"] = STAN_LINIA_ODJAZD
    await wyslij(
        update,
        "🚏 O której odjazd?\n\nNapisz numer linii (np. 17, 180, 15, N01):",
        InlineKeyboardMarkup([wiersz_menu()]),
        nowa,
    )


async def pokaz_kierunki(update, context, linia, nowa=True):
    kierunki = await asyncio.to_thread(gtfs_store.pobierz_kierunki, linia)
    if not kierunki:
        await wyslij(
            update,
            f"❌ Nie znalazłem linii {linia}. Sprawdź numer i spróbuj jeszcze raz.",
            InlineKeyboardMarkup([wiersz_menu(przycisk("🔄 Inna linia", "n_odjazd"))]),
            nowa,
        )
        return

    context.user_data["stan"] = None
    klawiatura = [[przycisk(f"➡️ {k['naglowek']}", f"dir:{k['kod']}")] for k in kierunki]
    klawiatura.append(wiersz_menu(przycisk("🔄 Inna linia", "n_odjazd")))
    await wyslij(
        update,
        f"Linia {linia}\nWybierz kierunek (krańcówkę):",
        InlineKeyboardMarkup(klawiatura),
        nowa,
    )


async def zaladuj_kierunek(context, kod):
    """Wczytuje kierunek i jego przystanki do user_data (z lokalnej bazy). False, gdy nie istnieje."""
    d = context.user_data
    if d.get("kierunek", {}).get("kod") == kod and d.get("przebieg"):
        return True
    kierunek = await asyncio.to_thread(gtfs_store.pobierz_kierunek, kod)
    if not kierunek:
        return False
    d["kierunek"] = kierunek
    d["przebieg"] = await asyncio.to_thread(gtfs_store.pobierz_przebieg, kod)
    d["linia"] = kierunek["linia"]
    return True


def tekst_naglowka_trasy(context):
    d = context.user_data
    return f"Linia {d['linia']} → {d['kierunek']['naglowek']}"


async def wygasniete(update):
    """Przycisk z bardzo starej wiadomości, której dane już nie istnieją po aktualizacji rozkładów."""
    await wyslij(
        update,
        "Ta lista jest już nieaktualna (rozkłady zostały zaktualizowane). Zacznij od nowa.",
        InlineKeyboardMarkup([wiersz_menu(przycisk("🔄 Inna linia", "n_odjazd"))]),
        nowa=True,
    )


async def pokaz_strone_przystankow(update, context, kod, strona, nowa):
    if not await zaladuj_kierunek(context, kod):
        await wygasniete(update)
        return
    d = context.user_data
    przebieg = d["przebieg"]
    liczba_stron = max(1, -(-len(przebieg) // PRZYSTANKOW_NA_STRONE))
    strona = max(0, min(strona, liczba_stron - 1))
    d["strona"] = strona
    d["stan"] = STAN_PRZYSTANEK

    od = strona * PRZYSTANKOW_NA_STRONE
    fragment = przebieg[od : od + PRZYSTANKOW_NA_STRONE]

    klawiatura = [
        [przycisk(f"{p['lp']}. {etykieta(p)}", f"stp:{kod}:{od + i}")]
        for i, p in enumerate(fragment)
    ]
    nawigacja = []
    if strona > 0:
        nawigacja.append(przycisk("◀️", f"pg:{kod}:{strona - 1}"))
    nawigacja.append(przycisk(f"{strona + 1}/{liczba_stron}", "noop"))
    if strona < liczba_stron - 1:
        nawigacja.append(przycisk("▶️", f"pg:{kod}:{strona + 1}"))
    klawiatura.append(nawigacja)
    klawiatura.append(wiersz_menu(przycisk("⬅️ Kierunki", f"bk:{kod}")))

    tekst = (
        f"{tekst_naglowka_trasy(context)}\n"
        f"Przystanki: {len(przebieg)}.\n\n"
        f"✍️ Najszybciej: napisz nazwę przystanku (lub jej fragment, z polskimi "
        f"znakami albo bez), a od razu podam odjazdy.\n"
        f"Albo wybierz z listy poniżej."
    )
    await wyslij(update, tekst, InlineKeyboardMarkup(klawiatura), nowa)


def znajdz_przystanki(przebieg, fraza):
    """
    Szuka przystanków po nazwie, ignorując wielkość liter i polskie znaki.
    Każde słowo z frazy musi występować w nazwie. Zwraca [(indeks, przystanek), ...];
    jeśli któryś przystanek ma nazwę identyczną z frazą, zwraca tylko dokładne trafienia.
    """
    szukane = normalizuj(fraza)
    slowa = szukane.split()
    if not slowa:
        return []
    trafione = []
    for i, p in enumerate(przebieg):
        nazwa = normalizuj(p["nazwa"])
        if all(s in nazwa for s in slowa):
            trafione.append((i, p))
    dokladne = [(i, p) for i, p in trafione if normalizuj(p["nazwa"]) == szukane]
    return dokladne or trafione


async def pokaz_wyniki_wyszukiwania(update, context, fraza):
    d = context.user_data
    kod = d["kierunek"]["kod"]
    trafione = znajdz_przystanki(d["przebieg"], fraza)
    lista_przystankow = przycisk("📋 Lista przystanków", f"lst:{kod}:{d.get('strona', 0)}")
    if not trafione:
        await update.message.reply_text(
            f"Nie znalazłem przystanku „{fraza}” na tej trasie. Spróbuj innej nazwy "
            f"albo wybierz z listy.",
            reply_markup=InlineKeyboardMarkup([wiersz_menu(lista_przystankow)]),
        )
        return
    if len(trafione) == 1:
        # jeden pasujący przystanek - od razu podajemy odjazdy
        await pokaz_odjazdy(update, context, kod, trafione[0][0], nowa=True)
        return
    klawiatura = [
        [przycisk(f"{p['lp']}. {etykieta(p)}", f"stp:{kod}:{i}")]
        for i, p in trafione[:PRZYSTANKOW_NA_STRONE]
    ]
    klawiatura.append(wiersz_menu(lista_przystankow))
    await update.message.reply_text(
        f"{tekst_naglowka_trasy(context)}\nPasujące przystanki:",
        reply_markup=InlineKeyboardMarkup(klawiatura),
    )


async def pokaz_odjazdy(update, context, kod, indeks, nowa):
    if not await zaladuj_kierunek(context, kod):
        await wygasniete(update)
        return
    d = context.user_data
    if not 0 <= indeks < len(d["przebieg"]):
        await wygasniete(update)
        return
    przystanek = d["przebieg"][indeks]
    kierunek = d["kierunek"]

    nazwa = etykieta(przystanek)
    teraz = datetime.now(ztm_api.STREFA)

    if kierunek.get("metro"):
        # API ZTM nie udostępnia odjazdów metra - liczymy je z rozkładu (odstępy między pociągami)
        godziny, odstep = await asyncio.to_thread(
            gtfs_store.odjazdy_metra, kod, przystanek["stop_id"], teraz, ILE_ODJAZDOW
        )
        if godziny:
            lista = "\n".join(f"🕒 {g}" for g in godziny)
        else:
            lista = "Brak kolejnych odjazdów w rozkładzie."
        liczba = f"{odstep:g}".replace(".", ",") if odstep else ""
        info_odstep = f" (metro kursuje teraz co ok. {liczba} min)" if odstep else ""
        tekst = (
            f"🚇 {nazwa}\n{tekst_naglowka_trasy(context)}\n\n"
            f"Najbliższe odjazdy, orientacyjnie{info_odstep}:\n{lista}\n\n"
            f"Stan na {teraz:%H:%M:%S}"
        )
        await wyslij_odjazdy(update, context, kod, indeks, tekst, nowa)
        return

    try:
        odjazdy = await asyncio.to_thread(
            ztm_api.pobierz_odjazdy, przystanek["zespol"], przystanek["slupek"], d["linia"]
        )
        blad = None
    except ztm_api.ZtmApiError:
        odjazdy, blad = [], "⚠️ API ZTM chwilowo nie odpowiada. Spróbuj za chwilę."

    if blad:
        tekst = f"🚏 {nazwa}\n{tekst_naglowka_trasy(context)}\n\n{blad}"
    else:
        najblizsze = await asyncio.to_thread(
            ztm_api.najblizsze_odjazdy, odjazdy, kierunek["naglowek"], ILE_ODJAZDOW
        )
        if najblizsze:
            lista = "\n".join(f"🕒 {godzina}" for godzina in najblizsze)
        else:
            lista = "Brak odjazdów tej linii z tego przystanku w rozkładzie na dziś."
        tekst = (
            f"🚏 {nazwa}\n{tekst_naglowka_trasy(context)}\n\n"
            f"Najbliższe odjazdy:\n{lista}\n\n"
            f"Stan na {teraz:%H:%M:%S}"
        )
    await wyslij_odjazdy(update, context, kod, indeks, tekst, nowa)


async def wyslij_odjazdy(update, context, kod, indeks, tekst, nowa):
    d = context.user_data
    strona = indeks // PRZYSTANKOW_NA_STRONE
    klawiatura = InlineKeyboardMarkup(
        [
            [przycisk("🔄 Odśwież", f"ref:{kod}:{indeks}")],
            [przycisk("📋 Lista przystanków", f"lst:{kod}:{strona}")],
            wiersz_menu(przycisk("⬅️ Kierunki", f"bk:{kod}")),
        ]
    )
    d["stan"] = STAN_PRZYSTANEK
    d["strona"] = strona
    await wyslij(update, tekst, klawiatura, nowa)


# --------------------------------------------------------------------------- #
# Utrudnienia drogowe (ogólne, bez przypisania do linii)
# --------------------------------------------------------------------------- #

async def pokaz_utrudnienia(update, context, nowa):
    try:
        utrudnienia = await asyncio.to_thread(ztm_api.pobierz_utrudnienia)
    except ztm_api.ZtmApiError:
        await wyslij(
            update,
            "⚠️ Nie udało się pobrać utrudnień. Spróbuj za chwilę.",
            InlineKeyboardMarkup([wiersz_menu(przycisk("🔄 Spróbuj ponownie", "ref_drogowe"))]),
            nowa,
        )
        return

    widoczne = [u for u in utrudnienia if u["id"] is not None][:MAKS_UTRUDNIEN]
    if not widoczne:
        tekst = "🟢 Brak aktywnych utrudnień drogowych."
        klawiatura = [wiersz_menu(przycisk("🔄 Odśwież", "ref_drogowe"))]
    else:
        tekst = (
            f"🚧 Aktywne utrudnienia drogowe: {len(utrudnienia)}\n"
            f"Pokazuję {len(widoczne)} najnowszych. Kliknij, aby zobaczyć szczegóły."
        )
        klawiatura = [[przycisk(skroc(u["nazwa"], 55), f"utr:{u['id']}")] for u in widoczne]
        klawiatura.append(wiersz_menu(przycisk("🔄 Odśwież", "ref_drogowe")))
    await wyslij(update, tekst, InlineKeyboardMarkup(klawiatura), nowa)


async def pokaz_szczegoly_utrudnienia(update, id_utrudnienia):
    try:
        wszystkie = await asyncio.to_thread(ztm_api.pobierz_utrudnienia, False)
    except ztm_api.ZtmApiError:
        wszystkie = []
    u = next((x for x in wszystkie if str(x["id"]) == id_utrudnienia), None)
    if u is None:
        tekst = "To utrudnienie nie jest już dostępne (mogło się zakończyć)."
    else:
        tresc = "\n\n".join(u["tresc"]) or "Brak szczegółów."
        tekst = (
            f"🚧 {u['nazwa']}\n"
            f"Od: {format_daty(u['start'])}\nDo: {format_daty(u['koniec'])}\n\n{tresc}"
        )
        tekst = skroc(tekst, 3800)
    await wyslij(
        update,
        tekst,
        InlineKeyboardMarkup([wiersz_menu(przycisk("🚨 Utrudnienia", "n_awarie"))]),
        nowa=True,
    )


# --------------------------------------------------------------------------- #
# Komunikaty o utrudnieniach: metro + wybrana linia
# --------------------------------------------------------------------------- #

async def pokaz_menu_utrudnien(update, context, nowa):
    """Najpierw status metra (zawsze), potem prośba o numer linii bus/tram."""
    context.user_data.clear()
    context.user_data["stan"] = STAN_LINIA_KOMUNIKATY

    klawiatura = []
    try:
        metro = await asyncio.to_thread(komunikaty.komunikaty_metra)
    except ztm_api.ZtmApiError:
        metro = None

    if metro is None:
        sekcja_metra = "🚇 Metro: nie udało się pobrać komunikatów."
    elif not metro:
        sekcja_metra = "🚇 Metro: 🟢 brak utrudnień."
    else:
        sekcja_metra = f"🚇 Metro: ⚠️ komunikaty ({len(metro)}):\n" + "\n".join(
            f"• {skroc(k['naglowek'], 90)}" for k in metro
        )
        klawiatura = [
            [przycisk(f"🚇 {skroc(k['naglowek'], 50)}", f"kom:{k['id']}")] for k in metro
        ]

    tekst = (
        f"{sekcja_metra}\n\n"
        "🚌🚋 Napisz numer linii autobusowej lub tramwajowej, "
        "aby sprawdzić jej utrudnienia (np. 17, 180, N01):"
    )
    klawiatura.append([przycisk("🚧 Utrudnienia drogowe w mieście", "menu_drogowe")])
    klawiatura.append(wiersz_menu())
    await wyslij(update, tekst, InlineKeyboardMarkup(klawiatura), nowa)


async def pokaz_komunikaty_linii(update, context, linia):
    linia = komunikaty.normalizuj_linie(linia)
    try:
        lista = await asyncio.to_thread(komunikaty.komunikaty_linii, linia)
    except ztm_api.ZtmApiError:
        await update.message.reply_text(
            "⚠️ Nie udało się pobrać komunikatów. Spróbuj za chwilę.",
            reply_markup=InlineKeyboardMarkup([wiersz_menu(przycisk("🔄 Spróbuj ponownie", "n_awarie"))]),
        )
        return

    context.user_data["stan"] = STAN_LINIA_KOMUNIKATY

    if lista:
        tekst = f"⚠️ Linia {linia}: komunikaty ({len(lista)}). Kliknij, aby zobaczyć szczegóły."
        klawiatura = [[przycisk(skroc(k["naglowek"], 55), f"kom:{k['id']}")] for k in lista]
    else:
        istnieje = (
            await asyncio.to_thread(gtfs_store.pobierz_kierunki, linia)
            if gtfs_store.baza_gotowa()
            else True
        )
        tekst = (
            f"🟢 Linia {linia}: brak komunikatów o utrudnieniach."
            if istnieje
            else f"❌ Nie znalazłem linii {linia}. Sprawdź numer i spróbuj jeszcze raz."
        )
        klawiatura = []
    tekst += "\n\nMożesz napisać numer innej linii."
    klawiatura.append(wiersz_menu(przycisk("🚇 Metro / inna linia", "n_awarie")))
    await update.message.reply_text(tekst, reply_markup=InlineKeyboardMarkup(klawiatura))


async def pokaz_szczegoly_komunikatu(update, id_komunikatu):
    try:
        k = await asyncio.to_thread(komunikaty.znajdz_komunikat, id_komunikatu)
    except ztm_api.ZtmApiError:
        k = None
    if k is None:
        tekst = "Ten komunikat nie jest już aktualny."
    else:
        tekst = f"🚨 {k['naglowek']}\n\n{k['opis'] or 'Brak dodatkowych szczegółów.'}"
        if k["linie"]:
            tekst += f"\n\nLinie: {', '.join(k['linie'][:20])}"
        tekst = skroc(tekst, 3800)
    await wyslij(
        update,
        tekst,
        InlineKeyboardMarkup([wiersz_menu(przycisk("🚨 Utrudnienia", "n_awarie"))]),
        nowa=True,
    )


# --------------------------------------------------------------------------- #
# Pogoda: teraz (stacje miejskie), za 4 godziny i jutro (Open-Meteo)
# --------------------------------------------------------------------------- #

async def pokaz_pogode(update, rodzaj, nowa):
    """rodzaj: 'teraz' | '4h' | 'jutro'."""
    try:
        if rodzaj == "teraz":
            tekst = await asyncio.to_thread(pogoda.tekst_teraz)
        elif rodzaj == "4h":
            tekst = await asyncio.to_thread(pogoda.tekst_za_godzin, 4)
        else:
            tekst = await asyncio.to_thread(pogoda.tekst_jutro)
    except ztm_api.ZtmApiError:
        tekst = "⚠️ Nie udało się pobrać danych pogodowych. Spróbuj za chwilę."

    przyciski_pogody = {
        "teraz": przycisk("🌡 Teraz", "pog:teraz"),
        "4h": przycisk("🕓 Za 4 godziny", "pog:4h"),
        "jutro": przycisk("📅 Jutro", "pog:jutro"),
    }
    inne = [p for r, p in przyciski_pogody.items() if r != rodzaj]
    klawiatura = [inne]
    if rodzaj == "teraz":
        klawiatura.append([przycisk("🔄 Odśwież", "ref_pogoda")])
    klawiatura.append(wiersz_menu())
    await wyslij(update, tekst, InlineKeyboardMarkup(klawiatura), nowa)


# --------------------------------------------------------------------------- #
# Automatyczne powiadomienia o utrudnieniach w metrze
# --------------------------------------------------------------------------- #

PLIK_METRO = os.path.join(gtfs_store.KATALOG, "metro_wyslane.json")
CO_ILE_SPRAWDZAC_METRO_S = 300


def _wczytaj_wyslane_metro():
    try:
        with open(PLIK_METRO, encoding="utf-8") as f:
            return set(json.load(f).get("ids", [])), True
    except (OSError, ValueError):
        return set(), False


def _zapisz_wyslane_metro(ids):
    os.makedirs(gtfs_store.KATALOG, exist_ok=True)
    with open(PLIK_METRO, "w", encoding="utf-8") as f:
        json.dump({"ids": sorted(ids)}, f)


async def rozeslij(application, tekst):
    for user_id in DOZWOLONE_ID:
        try:
            await application.bot.send_message(chat_id=user_id, text=tekst)
        except TelegramError as e:
            # np. użytkownik jeszcze nie napisał /start do bota albo zablokował bota
            logger.warning("Nie udało się wysłać powiadomienia do %s: %s", user_id, e)


async def sprawdz_metro(application):
    try:
        metro = await asyncio.to_thread(komunikaty.komunikaty_metra)
    except ztm_api.ZtmApiError:
        return  # nie zmieniamy stanu, spróbujemy za kilka minut

    wyslane, plik_istnieje = _wczytaj_wyslane_metro()
    aktualne = {k["id"]: k for k in metro}

    for id_k, k in aktualne.items():
        if id_k not in wyslane:
            opis = k["opis"] or ""
            tekst = skroc(f"🚇 Utrudnienia w metrze\n\n{k['naglowek']}\n\n{opis}".strip(), 3800)
            await rozeslij(application, tekst)

    if plik_istnieje and wyslane and not aktualne:
        await rozeslij(application, "🚇 Metro: 🟢 utrudnienia zakończone, brak aktywnych komunikatów.")

    if set(aktualne) != wyslane:
        _zapisz_wyslane_metro(set(aktualne))


async def petla_powiadomien_metro(application):
    await asyncio.sleep(20)  # chwila na start bota
    while True:
        try:
            await sprawdz_metro(application)
        except Exception:
            logger.exception("Błąd sprawdzania komunikatów o metrze")
        await asyncio.sleep(CO_ILE_SPRAWDZAC_METRO_S)


# --------------------------------------------------------------------------- #
# Obsługa kliknięć
# --------------------------------------------------------------------------- #

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not czy_ma_dostep(update.effective_user.id):
        await query.answer("Brak dostępu!", show_alert=True)
        return

    await query.answer()
    data = query.data
    czesci = data.split(":")
    akcja = czesci[0]

    try:
        if data == "noop":
            return

        # --- menu (edycja = przejście z menu głównego; n_* = nowa wiadomość z wyniku) ---
        if data == "menu_start":
            await pokaz_menu(update, context, nowa=True)
        elif data == "menu_odjazd":
            await pokaz_prompt_odjazd(update, context, nowa=False)
        elif data == "n_odjazd":
            await pokaz_prompt_odjazd(update, context, nowa=True)
        elif data == "menu_awarie":
            await pokaz_menu_utrudnien(update, context, nowa=False)
        elif data == "n_awarie":
            await pokaz_menu_utrudnien(update, context, nowa=True)

        # --- pogoda ---
        elif data == "menu_pogoda":
            await pokaz_pogode(update, "teraz", nowa=False)
        elif data == "ref_pogoda":
            await pokaz_pogode(update, "teraz", nowa=False)
        elif akcja == "pog":
            await pokaz_pogode(update, czesci[1], nowa=True)

        # --- utrudnienia drogowe i komunikaty ---
        elif data == "menu_drogowe":
            await pokaz_utrudnienia(update, context, nowa=True)
        elif data == "ref_drogowe":
            await pokaz_utrudnienia(update, context, nowa=False)
        elif akcja == "utr":
            await pokaz_szczegoly_utrudnienia(update, czesci[1])
        elif akcja == "kom":
            await pokaz_szczegoly_komunikatu(update, czesci[1])

        # --- odjazdy ---
        elif akcja == "dir":   # wybór kierunku -> lista przystanków (edycja w miejscu)
            await pokaz_strone_przystankow(update, context, int(czesci[1]), 0, nowa=False)
        elif akcja == "pg":    # zmiana strony listy (edycja w miejscu)
            await pokaz_strone_przystankow(update, context, int(czesci[1]), int(czesci[2]), nowa=False)
        elif akcja == "lst":   # lista przystanków wywołana z wyniku (nowa wiadomość)
            await pokaz_strone_przystankow(update, context, int(czesci[1]), int(czesci[2]), nowa=True)
        elif akcja == "bk":    # powrót do wyboru kierunku (nowa wiadomość)
            if await zaladuj_kierunek(context, int(czesci[1])):
                await pokaz_kierunki(update, context, context.user_data["linia"], nowa=True)
            else:
                await wygasniete(update)
        elif akcja == "stp":   # wynik: odjazdy z przystanku (nowa wiadomość, zostaje w historii)
            await pokaz_odjazdy(update, context, int(czesci[1]), int(czesci[2]), nowa=True)
        elif akcja == "ref":   # odświeżenie odjazdów (edycja w miejscu)
            await pokaz_odjazdy(update, context, int(czesci[1]), int(czesci[2]), nowa=False)
    except (ValueError, IndexError):
        logger.warning("Nieprawidłowe dane przycisku: %s", data)


# --------------------------------------------------------------------------- #
# Obsługa wiadomości tekstowych
# --------------------------------------------------------------------------- #

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not czy_ma_dostep(update.effective_user.id):
        await update.message.reply_text("⛔ Brak dostępu. Ten bot jest prywatny.")
        return

    tekst = update.message.text.strip()
    stan = context.user_data.get("stan")

    if stan == STAN_LINIA_ODJAZD:
        linia = tekst.upper()
        if not re.fullmatch(r"[A-Z0-9]{1,6}", linia):
            await update.message.reply_text("Podaj sam numer linii, np. 17 albo N01.")
            return
        await pokaz_kierunki(update, context, linia)

    elif stan == STAN_LINIA_KOMUNIKATY:
        linia = komunikaty.normalizuj_linie(tekst)
        if not re.fullmatch(r"[A-Z0-9]{1,6}", linia):
            await update.message.reply_text("Podaj sam numer linii, np. 17 albo N01.")
            return
        await pokaz_komunikaty_linii(update, context, linia)

    elif stan == STAN_PRZYSTANEK and context.user_data.get("przebieg"):
        await pokaz_wyniki_wyszukiwania(update, context, tekst)

    else:
        await update.message.reply_text("Użyj komendy /start, aby otworzyć menu bota.")


# --------------------------------------------------------------------------- #
# Aktualizacja bazy tras w tle i uruchomienie
# --------------------------------------------------------------------------- #

async def petla_aktualizacji_tras():
    """Przy starcie i potem co kilka godzin sprawdza, czy baza tras nie jest starsza niż doba."""
    while True:
        try:
            if await asyncio.to_thread(gtfs_store.aktualizuj):
                logger.info("Baza tras zaktualizowana.")
        except Exception:
            logger.exception("Nie udało się zaktualizować bazy tras")
        await asyncio.sleep(600 if not gtfs_store.baza_gotowa() else 6 * 3600)


async def po_starcie(application: Application):
    application.bot_data["zadanie_gtfs"] = asyncio.create_task(petla_aktualizacji_tras())
    application.bot_data["zadanie_metro"] = asyncio.create_task(petla_powiadomien_metro(application))


async def obsluga_bledow(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Nieobsłużony wyjątek", exc_info=context.error)


def main():
    if not TELEGRAM_TOKEN:
        print("❌ BŁĄD: Brak TELEGRAM_TOKEN w pliku .env!")
        return
    if not os.getenv("API_TOKEN"):
        print("❌ BŁĄD: Brak API_TOKEN w pliku .env!")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).post_init(po_starcie).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(obsluga_bledow)

    print("🤖 Bot gotowy. Trasy z lokalnej bazy, odjazdy i utrudnienia z API na żądanie.")
    application.run_polling()


if __name__ == "__main__":
    main()
