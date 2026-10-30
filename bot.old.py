import os
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from dotenv import load_dotenv

# Import z Twojego pliku users.py
from users import DOZWOLONE_ID

# Załaduj zmienne środowiskowe
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_ZTM_TOKEN = os.getenv("API_TOKEN")

# Konfiguracja logowania
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

def czy_ma_dostep(user_id):
    """Sprawdza, czy użytkownik znajduje się na liście dozwolonych."""
    return user_id in DOZWOLONE_ID

# Słownik stanów użytkowników oraz ich danych tymczasowych
user_states = {}
user_data_cache = {}


def pobierz_krancowki_z_rozkladu(linia):
    """
    Odpytuje oficjalny endpoint rozkładowy ZTM (dbtimetable_get) WYŁĄCZNIE na żądanie.
    Wyciąga z rozkładu nazwy ostatnich przystanków (krańcówek) dla danej linii.
    Posiada automatyczne ponawianie w przypadku zerwania socketu przez UM.
    """
    url = "https://api.um.warszawa.pl/api/action/dbtimetable_get"
    szukana_linia = str(linia).upper().strip()
    
    # Oficjalne ID zasobu dla rozkładów przystankowych w Warszawie
    params = {
        "id": "88cd555f-6f31-43ca-9de4-66c479ef5942",
        "line": szukana_linia,
        "apikey": API_ZTM_TOKEN
    }
    
    znalezione_krancowki = set()

    # Robimy 3 próby na wypadek, gdyby serwer ZTM uciął połączenie (RemoteDisconnected)
    for proba in range(3):
        try:
            response = requests.post(url, data=params, timeout=10)
            if response.status_code == 200:
                dane = response.json()
                wynik = dane.get("result", []) if isinstance(dane, dict) else dane
                
                warianty = {}
                if isinstance(wynik, list):
                    for p in wynik:
                        elements = p.get("values", [])
                        wariant_id = None
                        nazwa_przystanku = None
                        
                        for el in elements:
                            if el.get("key") == "wariant":
                                wariant_id = el.get("value")
                            elif el.get("key") == "nazwa":
                                nazwa_przystanku = el.get("value")
                        
                        if wariant_id and nazwa_przystanku:
                            if wariant_id not in warianty:
                                warianty[wariant_id] = []
                            warianty[wariant_id].append(nazwa_przystanku)
                    
                    # Dla każdego wariantu trasy bierzemy OSTATNI przystanek (krańcówkę)
                    for wariant, przystanki in warianty.items():
                        if przystanki:
                            znalezione_krancowki.add(przystanki[-1])
                
                # Jeśli udało się coś pobrać, wychodzimy z pętli prób
                if znalezione_krancowki:
                    break
                    
        except Exception as e:
            logger.warning(f"Próba {proba+1} - Błąd komunikacji z endpointem rozkładów dla linii {linia}: {e}")
            
    return list(znalezione_krancowki)[:4]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wyświetla menu główne z przyciskami i resetuje stan."""
    user_id = update.effective_user.id
    
    if not czy_ma_dostep(user_id):
        if update.message:
            await update.message.reply_text("⛔ Brak dostępu. Ten bot jest prywatny.")
        return

    user_states[user_id] = None
    user_data_cache[user_id] = {}

    keyboard = [
        [InlineKeyboardButton("🚏 O której odjazd?", callback_data="menu_odjazd")],
        [InlineKeyboardButton("🚨 Awarie / Status linii", callback_data="menu_awarie")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = "🏠 Menu główne\nWybierz, co chcesz sprawdzić:"
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text=text, reply_markup=reply_markup)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Obsługuje kliknięcia w przyciski."""
    user_id = update.effective_user.id
    
    if not czy_ma_dostep(user_id):
        await query.answer("Brak dostępu!", show_alert=True)
        return

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_odjazd":
        user_states[user_id] = "czekam_na_linie_odjazd"
        keyboard = [[InlineKeyboardButton("⬅️ Powrót do menu", callback_data="menu_start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="🚏 O której odjazd?\n\nNapisz w wiadomości numer linii, którą chcesz sprawdzić (np. 17, 180, 15):",
            reply_markup=reply_markup,
        )

    elif data == "menu_awarie":
        user_states[user_id] = "czekam_na_linie_awaria"
        keyboard = [[InlineKeyboardButton("⬅️ Powrót do menu", callback_data="menu_start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="🚨 Awarie i utrudnienia\n\nNapisz numer linii, dla której chcesz sprawdzić status:",
            reply_markup=reply_markup,
        )

    elif data.startswith("kierunek_"):
        kierunek_nazwa = data.replace("kierunek_", "")
        linia = user_data_cache.get(user_id, {}).get("linia", "")
        
        keyboard = [[InlineKeyboardButton("🔄 Wybierz inną linię", callback_data="menu_odjazd"),
                     InlineKeyboardButton("🏠 Menu główne", callback_data="menu_start")]]
        
        await query.edit_message_text(
            text=f"Linia {linia} -> Wybrano kierunek (krańcówkę): {kierunek_nazwa}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        user_states[user_id] = None

    elif data == "menu_start":
        await start(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Obsługuje wiadomości tekstowe wpisane przez użytkownika na żądanie."""
    user_id = update.effective_user.id
    
    if not czy_ma_dostep(user_id):
        await update.message.reply_text("⛔ Brak dostępu. Ten bot jest prywatny.")
        return

    tekst = update.message.text.strip()
    stan = user_states.get(user_id)

    if stan == "czekam_na_linie_odjazd":
        linia = tekst
        user_data_cache[user_id] = {"linia": linia}
        user_states[user_id] = None
        
        await update.message.reply_text(f"🔍 Pobieram krańcówki z rozkładu dla linii {linia}...")
        
        # Zapytanie idzie DOPIERO TERAZ, wyłącznie na żądanie po podaniu numeru
        krancowki = pobierz_krancowki_z_rozkladu(linia)
        
        keyboard = []
        if krancowki:
            for k in krancowki:
                keyboard.append([InlineKeyboardButton(k, callback_data=f"kierunek_{k}")])
        else:
            keyboard.append([InlineKeyboardButton("❌ Brak danych rozkładowych dla tej linii", callback_data="menu_odjazd")])

        keyboard.append([InlineKeyboardButton("🏠 Menu główne", callback_data="menu_start")])
        
        await update.message.reply_text(
            f"Linia {linia}.\nWybierz kierunek (krańcówkę):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif stan == "czekam_na_linie_awaria":
        await update.message.reply_text(f"🚨 Sprawdzam status dla linii {tekst}...")
        
        krancowki = pobierz_krancowki_z_rozkladu(tekst)
        if krancowki:
            odpowiedz = f"🟢 Linia {tekst} jest aktywna w bazie rozkładowej."
        else:
            odpowiedz = f"⚠️ Uwaga: Brak rozkładu dla linii {tekst}."

        keyboard = [[InlineKeyboardButton("🏠 Menu główne", callback_data="menu_start")]]
        await update.message.reply_text(odpowiedz, reply_markup=InlineKeyboardMarkup(keyboard))
        user_states[user_id] = None

    else:
        await update.message.reply_text("Użyj komendy /start, aby otworzyć menu główne bota.")


def main():
    if not TELEGRAM_TOKEN:
        print("❌ BŁĄD: Brak TELEGRAM_TOKEN w pliku .env!")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot gotowy. Pobiera czyste nazwy krańcówek z rozkładu na żądanie.")
    application.run_polling()

if __name__ == "__main__":
    main()