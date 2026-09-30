# mysttic-diets

Baza dań Kuchni Vikinga. Dla każdego dania są składniki, alergeny, makro dla każdej kaloryczności i oceny klientów. Do tego menu dzień po dniu i dodatki.

Baza rośnie sama. Dwa razy w tygodniu GitHub Actions pobiera aktualne menu, dopisuje nowe dania bez dubli i commituje zmiany.

Stan startowy (2026-09-29): 1269 dań, 88 dodatków, 14 diet, menu 2026-10-02 → 2026-10-14.

## Przeglądanie

Zrób `git pull`, potem otwórz `przegladarka.html` w przeglądarce. Strona działa z dysku, bez serwera. Ma zakładki:

| Zakładka | Co zawiera |
|---|---|
| **Dania** | wyszukiwarka po nazwie i składnikach; filtry: pora, dieta, kcal, białko, ocena, „bez składnika”, „nowe (14 dni)”; szczegóły ze składnikami i makro dla każdej kaloryczności |
| **Diety** | opisy, warianty, ceny, oceny |
| **Menu dzienne** | dzień × dieta |
| **Moja historia** | tylko lokalnie, gdy masz `prywatne/` (patrz [Historia zamówień](#historia-zamówień-tylko-lokalnie)): zamówienie → dni → posiłki; w zakładce Dania dochodzi kolumna i filtr „jadłem” |
| **Dodatki** | dodatki do menu |
| **Losuj dzień** | losowanie posiłków pod cel kcal; kłódka blokuje wybrane danie |

### Online (GitHub Pages)

Adres: https://mysttic.github.io/mysttic-diets/

- **Ta sama przeglądarka, bez historii.** `scripts/strona.py` kopiuje `przegladarka.html` jako `index.html` razem z `dane.js`, usuwa odwołanie do `prywatne/dane.js` i dokleja `noindex`. Przerywa build, jeśli w `dane.js` znajdzie ślady historii zamówień.
- **Wdrożenie:** workflow `.github/workflows/strona.yml` publikuje stronę po każdym udanym *Aktualizuj menu* i po pushu na `main`, który zmienia przeglądarkę albo `dane.js`. Ręcznie: Actions → *Strona (GitHub Pages)* → *Run workflow*.
- **Jednorazowo:** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
- **Podgląd lokalny:** `python scripts/strona.py` buduje `_site/` (poza gitem).

## Aktualizacja

- **Automatycznie:** workflow `.github/workflows/aktualizuj-menu.yml` rusza w poniedziałki i czwartki o 03:23 UTC. Commit powstaje tylko wtedy, gdy coś się zmieniło, np. `menu 2026-10-05→2026-10-19: +23 dań, +1 dodatków (1292 dań w bazie)`. Surowe odpowiedzi API zostają w artefaktach runu przez 14 dni.
- **Ręcznie na GitHubie:** Actions → *Aktualizuj menu* → *Run workflow*.
- **Lokalnie:** `python scripts/pobierz.py`. Wystarczy Python 3.8+, bez dodatkowych pakietów.
- **Częstotliwość:** zmieniasz linię `cron` w workflow.

KV publikuje menu tylko ok. 2 tygodnie do przodu. Wstecz nie da się go pobrać, więc archiwum rośnie wyłącznie dzięki regularnemu pobieraniu. Każde uruchomienie dopisuje się do `data/log.csv`: ile przyszło nowych dań, ile wierszy menu dodano i usunięto.

## Deduplikacja

- **Danie** to znormalizowana nazwa: wielkość liter, spacje, cudzysłowy i myślniki nie tworzą nowego dania. Każde danie ma stałe `id`. Nowe dania trafiają na koniec listy, nic nie jest usuwane.
- **Źródło opisu.** Ta sama nazwa bywa w kilku dietach z inną recepturą (np. wersja keto), a w różne dni ma inne porcje, bo KV bilansuje kaloryczność dnia. Składniki i makro dania pochodzą więc zawsze z jednego źródła, zapisanego w polu `zrodlo_opisu`. Brana jest obserwacja z najpóźniejszego dnia menu.
- **Menu.** Para (data, dieta) z nowszego pobrania zastępuje starsze wiersze tej pary. W jej obrębie wiersz jest unikalny po (pora, danie). Menu bez wariantu 2000 kcal nie nadpisuje menu z tym wariantem.
- **Dodatki** są dopasowywane po id z API. Dodatek, którego już nie ma w ofercie, dostaje `dostepny: false`.
- **Starsze pliki** (sprzed ostatniej aktualizacji) tylko uzupełniają braki. Ponowne scalenie tego samego pliku nie zmienia ani bajtu.
- **Zaślepki** „Posiłek z wariantu …”, które API zwraca dla dni spoza opublikowanego menu, są pomijane.

## Pliki

| plik | opis |
|---|---|
| `przegladarka.html` + `dane.js` | przeglądarka bazy; `dane.js` jest generowany |
| `data/dania.json` | **baza dań** (źródło prawdy): jedno danie na linię, stałe id, `warianty` = makro dla każdej kaloryczności |
| `data/dania.csv` | to samo spłaszczone do Excela |
| `data/menu.csv` | menu dzień × dieta × pora × danie, z makro wariantu referencyjnego |
| `data/dodatki.json` / `.csv` | dodatki do menu |
| `data/diety.json` / `.csv` | oferta diet: warianty kcal, ceny, oceny, opisy |
| `data/log.csv`, `data/meta.json` | log aktualizacji, liczniki |
| `scripts/pobierz.py` | pobiera bieżące menu z publicznego API → `raw/menu_<czas>.json.gz` → `scal.py` |
| `scripts/scal.py` | scala surowe pliki z `data/` i generuje eksporty (`python scripts/scal.py` bez argumentów tylko przebudowuje eksporty) |
| `scripts/aktualizuj.ps1` | lokalna alternatywa dla Actions |
| `scripts/strona.py` | strona dla GitHub Pages → `_site/` |
| `raw/` | surowe odpowiedzi API; poza gitem |
| `prywatne/` | historia zamówień i jej nakładka dla przeglądarki; poza gitem (patrz niżej) |

Pliki CSV mają separator `;` i kodowanie UTF-8 z BOM, więc Excel otwiera je bez importu. Listy w komórkach są rozdzielone ` | `.

## Źródła

Wszystko przez `https://panel.kuchniavikinga.pl/api/panel/open/…`, bez logowania. Menu jest takie samo w całej Polsce: sprawdzone na 10 miastach, więc miasto jest dowolne; używane jest `cityId=918123` (Warszawa).

| endpoint | co daje |
|---|---|
| `order-form/diet-details?cityId=` | diety, tiery Wyboru menu (BASIC, COMFORT, SUPREME, LADIES VIBES, KETO FUSION, TYPES OF VEGE), kaloryczności z `dietCaloriesId` i `dietCaloriesMealIds`, ceny |
| `order-form/steps/example-menu?dietId=&cityId=&date=` | menu diet gotowych; API podaje makro tylko dla 1200 kcal |
| `order-form/steps/menu-configuration/meals?dietCaloriesId=&cityId=&date=&dietCaloriesMealIds=` | menu tieru w danej kaloryczności |
| `order-form/steps/menu-configuration/settings?dietCaloriesId=&cityId=` | okno dat z opublikowanym menu |
| `order-form/steps/side-orders?cityId=` | dodatki |

## Historia zamówień (tylko lokalnie)

Historia wymaga zalogowania do panelu (`/api/company/customer/order/…`), więc automat jej nie pobiera, a repo jej nie przechowuje. Wszystko z nią związane żyje w folderze `prywatne/`, który jest w `.gitignore`:

| plik | opis |
|---|---|
| `prywatne/historia.csv`, `prywatne/zamowienia.json` | zamówienia → dni → posiłki |
| `prywatne/dania.json` | dania z historii, których w chwili importu nie było w publicznej bazie |
| `prywatne/dane.js` | nakładka dla `przegladarka.html` (zakładka *Moja historia*, „jadłem”, waga porcji) |

- **Import:** plik z polem `history` (zrzut z panelu klienta) scalasz komendą `python scripts/scal.py raw/<plik>.json`. Trafia wyłącznie do `prywatne/`. Zamówienia z pliku zastępują swoje wcześniejsze wiersze, pozostałe zostają, a plik starszy niż ostatni import jest pomijany. `prywatne/` nie ma kopii w git, więc rób jej kopię zapasową.
- **Publiczne pliki nigdy od niej nie zależą.** `scal.py` z `prywatne/` i bez niego daje bajt w bajt te same `data/` i `dane.js`. Dania z historii są łączone z bazą po nazwie; danie spoza bazy dostaje prywatne id i jest tylko w `prywatne/`.
- **Nakładka odświeża się** przy każdym `scal.py`/`pobierz.py`, więc danie, które później wejdzie do menu, samo przechodzi na publiczne id.
- **Bez `prywatne/`** (np. na GitHub Pages) strona działa normalnie, tylko bez historii.

## Gdy Actions nie może pobrać menu

Nieudany run (`BŁĄD: nie udało się pobrać …`, kod 2) jest czerwony w zakładce Actions, a GitHub wysyła wtedy maila. Jeśli KV zacznie blokować serwery GitHuba, możesz pobierać lokalnie z Harmonogramu zadań Windows:

```
schtasks /Create /SC WEEKLY /D MON,THU /ST 07:00 /TN mysttic-diets /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\sciezka\do\mysttic-diets\scripts\aktualizuj.ps1"
```

W takim przypadku usuń blok `schedule:` z workflow.
