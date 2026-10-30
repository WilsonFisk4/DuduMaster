"""
MOJE MIEJSCA - tutaj definiujesz swoje stałe miejsca (dom, praca, sklep, szkoła...).

Każde miejsce ma NAZWĘ (to, co zobaczysz na przycisku w bocie) i opis,
który przystanek Cię interesuje oraz jakie linie.

NAJŁATWIEJ:
  1. Otwórz plik  katalog_przystankow.csv  (w Excelu, LibreOffice albo Arkuszach Google).
     Jest tam lista WSZYSTKICH przystanków z ulicą, liniami i kierunkami.
     Znajdź swój przystanek (Ctrl+F po nazwie albo ulicy) i skopiuj z kolumny
     "ID (do miejsca.py)" wartość, np. 701301.
     Jeden przystanek ma kilka słupków (po jednym na kierunek/stronę ulicy) - wybierz ten,
     przy którym w kolumnie "Linie i kierunki" jest Twoja linia i właściwy kierunek.
  2. Wklej go poniżej jako "przystanek_id" i wpisz nazwę miejsca.

PRZYKŁAD - tak wygląda jedno miejsce (usuń znaki # z początku linii, żeby go użyć):

#    "Dom": {
#        "przystanek_id": "701301",
#        "linie": ["175"],
#    },

Pola:
  "przystanek_id" - ID z katalogu (6 cyfr dla autobusu/tramwaju, np. "701301";
                    dla metra np. "7013M:P1")
  "linie"         - (opcjonalnie) tylko te linie, które Cię interesują, np. ["17", "180"].
                    Pomiń to pole albo wpisz None, żeby bot pokazał wszystkie linie z tego słupka.

Zamiast "przystanek_id" możesz też podać (nie musisz używać katalogu):
  "przystanek"   - nazwa przystanku, np. "Przy Agorze"
  "wspolrzedne"  - (szerokosc, dlugosc), np. (52.2297, 21.0122) - bot wybierze najbliższy przystanek
  (wtedy dodaj też "kierunek": "nazwa krańcówki", jeśli jedziesz w jedną stronę)

Nazwy miejsc (np. "Dom", "Praca", "Sklep") możesz wybrać dowolnie i dowolnie dopisywać kolejne.
Katalog przystanków możesz odświeżyć w każdej chwili komendą:  python generuj_katalog.py
"""

MIEJSCA = {
    # Dopisz tutaj swoje miejsca wg wzoru z opisu powyżej, np.:
    #
    # "Dom": {
    #     "przystanek_id": "701301",
    #     "linie": ["175"],
    # },
    #
    # "Praca": {
    #     "przystanek_id": "701306",
    # },
    #
    # "Sklep": {
    #     "przystanek": "Przy Agorze",
    #     "linie": ["17"],
    #     "kierunek": "PKP Służewiec",
    # },
}
