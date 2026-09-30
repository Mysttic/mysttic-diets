#!/usr/bin/env python3
"""Scala surowe odpowiedzi API Kuchni Vikinga z bazą w data/ — bez dubli — i generuje eksporty.

Użycie:
  python scripts/scal.py raw/menu_2026-10-05T0323Z.json.gz [...]   scal pliki (w kolejności pobrania) i przebuduj eksporty
  python scripts/scal.py                                            tylko przebuduj eksporty z data/

Deduplikacja:
  • danie = znormalizowana nazwa (wielkość liter, białe znaki, cudzysłowy, myślniki) → jeden rekord ze stałym id;
    nowe dania są dopisywane na koniec (id = max + 1), nic nie jest usuwane; makro, składniki i oceny
    aktualizują się do najnowszych
  • menu: para (data, dieta) z nowszego pobrania zastępuje starsze wiersze tej pary, a w jej obrębie wiersz
    jest unikalny po (pora, danie_id) — ponowne pobranie tych samych dni niczego nie dubluje
  • dodatki: po id z API
  • historia zamówień (plik bundle z polem history, wymaga zalogowania do panelu) trafia wyłącznie do prywatne/
    (poza gitem) — publiczne pliki nigdy od niej nie zależą: przebieg z prywatne/ i bez daje identyczne data/ i dane.js
  • zaślepki „Posiłek z wariantu …” (daty spoza opublikowanego menu) są pomijane
  • ta sama nazwa bywa w kilku dietach z inną recepturą (np. wersja keto) — składniki i makro dania pochodzą
    zawsze z jednego źródła (zrodlo_opisu = dieta/etykieta), więc rekord nie przeskakuje między przepisami
  • plik starszy niż ostatnia aktualizacja bazy tylko uzupełnia braki (nie nadpisuje nowszych danych),
    więc kolejność scalania nie ma znaczenia, a ponowne scalenie tego samego pliku niczego nie zmienia
"""
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
RAW = os.path.join(ROOT, 'raw')
PRYW = os.path.join(ROOT, 'prywatne')  # historia zamówień — tylko lokalnie, w .gitignore
PRYW_ID = 1_000_000  # id dań spoza publicznej bazy (tylko w prywatne/dane.js)
REF_KCAL = 2000  # wariant, którego makro trafia do głównych kolumn (gdy dostępny)
PORY = ['Śniadanie', 'II śniadanie', 'Obiad', 'Podwieczorek', 'Kolacja']
TERMIKA = {'WARM': 'na ciepło', 'COLD': 'na zimno', 'COLD_WARM': 'ciepło/zimno'}
ZASLEPKA = re.compile(r'^\s*posiłek z wariantu', re.I)
MAKRO = [('kcal', 'calories'), ('bialko', 'protein'), ('tluszcz', 'fat'), ('wegle', 'carbohydrate'),
         ('blonnik', 'dietaryFiber'), ('cukry', 'sugar'), ('sol', 'salt'), ('nasycone', 'saturatedFattyAcids')]
MAKRO_KOL = ['kcal', 'bialko_g', 'tluszcz_g', 'wegle_g', 'blonnik_g', 'cukry_g', 'sol_g', 'tluszcze_nasycone_g']

MENU_KOL = ['data', 'dieta', 'dietId', 'kcal_diety', 'etykieta', 'pora', 'danie_id', 'danie'] + MAKRO_KOL + \
           ['termika', 'pobrano']
HIST_KOL = ['data', 'zamowienie', 'program', 'wariant_diety', 'kcal_diety', 'posilkow_dziennie', 'pora', 'danie_id', 'danie',
            'dieta_dania', 'ilosc'] + MAKRO_KOL + ['waga_g', 'termika']
LOG_KOL = ['pobrano', 'plik', 'dni_menu', 'menu_od', 'menu_do', 'pozycji', 'nowe_dania', 'menu_nowe', 'menu_usuniete',
           'nowe_dodatki', 'bledy_pobierania', 'dan_w_bazie']
POLA_DANIA = ['id', 'danie', 'klucz', 'pory', 'diety', 'etykiety', 'makro', 'makro_kcal', 'warianty',
              'termika', 'skladniki', 'skladniki_glowne', 'alergeny', 'zrodlo_opisu', 'ocena_proc', 'liczba_ocen',
              'ocena_z_dnia', 'dodano', 'w_menu_daty']


# ---------------------------------------------------------------- pomocnicze

def klucz(nazwa):
    """Klucz deduplikacji dania: różnice w wielkości liter, spacjach, cudzysłowach i myślnikach nie tworzą nowego dania."""
    s = unicodedata.normalize('NFKC', nazwa or '').lower()
    s = s.replace("''", '"').replace('``', '"')
    s = re.sub(r'[“”„‟«»″]', '"', s)
    s = re.sub(r'[‘’‚‛`´′]', "'", s)
    s = re.sub(r'[‐‑‒–—―−]', '-', s)
    s = re.sub(r'\s*-\s*', ' - ', s)
    s = re.sub(r'\s*([,;:])\s*', r'\1 ', s)
    s = re.sub(r'\(\s+', '(', s)
    s = re.sub(r'\s+\)', ')', s)
    return re.sub(r'\s+', ' ', s).strip(' .,;')


def liczba(v):
    """'448.26 kcal / 1875 kJ' → 448.26, '39.55g' → 39.55, 208.0 → 208; brak → None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        m = re.search(r'-?\d+(?:[.,]\d+)?', str(v))
        if not m:
            return None
        x = float(m.group(0).replace(',', '.'))
    x = round(x, 2)
    return int(x) if x.is_integer() else x


def makro_z(src):
    src = src or {}
    return {k: liczba(src.get(api)) for k, api in MAKRO}


def ustal_makro(d):
    """makro i makro_kcal = wariant referencyjny (albo najbliższy) z d['warianty']."""
    d['warianty'] = dict(sorted(d['warianty'].items(), key=lambda kv: int(kv[0])))
    ref = wariant_ref(d['warianty'])
    d['makro'] = d['warianty'][ref] if ref else {k: None for k, _ in MAKRO}
    d['makro_kcal'] = int(ref) if ref else None


def danie_js(d, **extra):
    """Danie w formacie dane.js (wspólne dla publicznej bazy i prywatnej nakładki)."""
    return {'id': d['id'], 'danie': d['danie'], 'pory': d['pory'], 'diety': d['diety'], 'etykiety': d['etykiety'],
            'makro': d['makro'], 'makro_dla_wariantu_kcal': d['makro_kcal'], 'warianty': d['warianty'],
            'termika': d['termika'], 'skladniki': d['skladniki'], 'skladniki_glowne': d['skladniki_glowne'],
            'alergeny': d['alergeny'], 'ocena_proc': d.get('ocena_proc'), 'liczba_ocen': d.get('liczba_ocen'),
            'dodano': d['dodano'], 'w_menu_daty': d.get('w_menu_daty') or [], **extra}


def pora_idx(p):
    return PORY.index(p) if p in PORY else len(PORY)


def dodaj(lista, v):
    if v and v not in lista:
        lista.append(v)


def realna_opcja(o):
    """Opcja z prawdziwym daniem (API zwraca zaślepki bez szczegółów dla dni spoza opublikowanego menu)."""
    return bool(o and o.get('name') and o.get('details') and not ZASLEPKA.match(o['name']))


def oczysc(tekst):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', tekst or '')).strip()


def sciezka(nazwa):
    return os.path.join(DATA, nazwa)


def wczytaj_json(p, domyslnie):
    if not os.path.exists(p):
        return domyslnie
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def wczytaj_surowe(p):
    otworz = gzip.open if p.endswith('.gz') else open
    with otworz(p, 'rt', encoding='utf-8') as f:
        return json.load(f)


def wczytaj_csv(p):
    if not os.path.exists(p):
        return []
    with open(p, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f, delimiter=';'))


def csv_tekst(kolumny, wiersze):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';', lineterminator='\n')
    w.writerow(kolumny)
    for r in wiersze:
        w.writerow(['' if r.get(k) is None else r.get(k) for k in kolumny])
    return '﻿' + buf.getvalue()  # BOM → Excel otwiera polskie znaki bez importu


def json_linie(lista):
    """Tablica JSON, jeden element na linię → czytelne diffy w git."""
    if not lista:
        return '[]\n'
    return '[\n' + ',\n'.join(json.dumps(x, ensure_ascii=False, separators=(',', ':')) for x in lista) + '\n]\n'


def zapisz(p, tekst):
    """Zapisuje tylko przy zmianie treści; zwraca True, jeśli plik się zmienił."""
    if os.path.exists(p):
        with open(p, encoding='utf-8', newline='') as f:
            if f.read() == tekst:
                return False
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write(tekst)
    return True


def na_liczbe(v):
    """Wartość z CSV (tekst) → liczba dla dane.js."""
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        x = float(v)
        return int(x) if x.is_integer() else x
    except ValueError:
        return v


def tresc_wiersza(r):
    """Wiersz menu jako krotka tekstów (bez daty pobrania) — do porównań niezależnych od typu (CSV vs API)."""
    return tuple('' if r.get(k) is None else str(r.get(k)) for k in MENU_KOL if k != 'pobrano')


def wariant_ref(warianty):
    if not warianty:
        return None
    if str(REF_KCAL) in warianty:
        return str(REF_KCAL)
    return min(warianty, key=lambda k: (abs(int(k) - REF_KCAL), -int(k)))


def wartosci_dodatku(opis):
    s = opis or ''

    def wez(wzor):
        m = re.search(wzor, s, re.I)
        return liczba(m.group(1)) if m else None
    return dict(kcal=wez(r'(\d+(?:[.,]\d+)?)\s*kcal'),
                bialko=wez(r'bia[łl]ko[^\d]{0,12}(\d+(?:[.,]\d+)?)\s*g'),
                tluszcz=wez(r't[łl]uszcz[^\d]{0,12}(\d+(?:[.,]\d+)?)\s*g'),
                wegle=wez(r'w[ęe]glowodany[^\d]{0,12}(\d+(?:[.,]\d+)?)\s*g'))


# ---------------------------------------------------------------- baza

class Baza:
    def __init__(self):
        self.dania = wczytaj_json(sciezka('dania.json'), [])
        self.po_kluczu = {d['klucz']: d for d in self.dania}
        self.nastepne_id = max((d['id'] for d in self.dania), default=0) + 1
        self.menu = defaultdict(list)  # (data, dieta) → wiersze
        for r in wczytaj_csv(sciezka('menu.csv')):
            self.menu[(r['data'], r['dieta'])].append(r)
        self.dodatki = wczytaj_json(sciezka('dodatki.json'), [])
        self.diety = wczytaj_json(sciezka('diety.json'), [])
        self.log = wczytaj_csv(sciezka('log.csv'))
        self.meta = wczytaj_json(sciezka('meta.json'), {})
        self.stat = Counter()
        self.nowsze = True  # False = plik starszy niż ostatnia aktualizacja: tylko uzupełnianie braków

    # -------- dania

    def danie(self, nazwa, dzien):
        k = klucz(nazwa)
        d = self.po_kluczu.get(k)
        if d is None:
            d = {'id': self.nastepne_id, 'danie': ' '.join(nazwa.split()), 'klucz': k, 'pory': [], 'diety': [],
                 'etykiety': [], 'warianty': {}, 'termika': '', 'skladniki': [],
                 'skladniki_glowne': [], 'alergeny': [], 'zrodlo_opisu': None, 'ocena_proc': None, 'liczba_ocen': None,
                 'ocena_z_dnia': None, 'dodano': dzien}
            self.nastepne_id += 1
            self.dania.append(d)
            self.po_kluczu[k] = d
            self.stat['nowe_dania'] += 1
        return d

    @staticmethod
    def _opis(d, thermo, skladniki, alergeny, tylko_puste=False):
        t = TERMIKA.get(thermo, thermo or '')
        if t and (not tylko_puste or not d.get('termika')):
            d['termika'] = t
        if skladniki:
            nazwy = [(i if isinstance(i, str) else i.get('name') or '').strip() for i in skladniki]
            nazwy = [n for n in nazwy if n]
            glowne = [(i.get('name') or '').strip() for i in skladniki if isinstance(i, dict) and i.get('major')]
            if nazwy and (not tylko_puste or not d.get('skladniki')):
                d['skladniki'], d['skladniki_glowne'] = nazwy, glowne
        if alergeny is not None:
            lista = alergeny.split(',') if isinstance(alergeny, str) else alergeny
            lista = [x.strip() for x in lista if x and x.strip()]
            if lista or not tylko_puste:
                if not tylko_puste or not d.get('alergeny'):
                    d['alergeny'] = lista

    def _zastosuj(self, d, obserwacje, dzien):
        """obserwacje: [(zrodlo, priorytet, data, kcal, dieta, opcja)] jednego dania z jednego pliku.
        priorytet 0 = menu-configuration (dokładniejsze liczby), 1 = example-menu.
        To samo danie ma w różne dni inne porcje (KV bilansuje kaloryczność dnia), więc opis bierzemy
        z jednej, deterministycznie wybranej obserwacji."""
        # w obrębie źródła: najpóźniejszy dzień (zostaje w kolejnych oknach → mniej zmian między pobraniami)
        obserwacje.sort(key=lambda x: (x[0], x[1], -dt.date.fromisoformat(x[2]).toordinal(), x[4], x[3] or 0))
        licznik = Counter(x[0] for x in obserwacje)
        zrodlo = d.get('zrodlo_opisu')
        przejmij = False
        if zrodlo not in licznik:
            if zrodlo is not None:
                zrodlo = None  # dotychczasowego źródła nie ma w tym pliku — opis zostaje bez zmian
            else:  # pierwsze dane z menu — wybierz najczęstsze źródło
                zrodlo = min(licznik, key=lambda z: (-licznik[z], z))
                przejmij = True
        if zrodlo is not None:
            nadpisz = self.nowsze or przejmij
            if przejmij:
                d['zrodlo_opisu'] = zrodlo
                d['warianty'] = {}
            wybrane = [x for x in obserwacje if x[0] == zrodlo]
            widziane = set()
            for _, _, _, kcal, _, o in wybrane:
                mak = makro_z(o['details'])
                if not kcal or mak['kcal'] is None or str(kcal) in widziane:
                    continue
                widziane.add(str(kcal))
                if nadpisz or str(kcal) not in d['warianty']:
                    d['warianty'][str(kcal)] = mak
            pierwsza = wybrane[0][5]
            self._opis(d, pierwsza['details'].get('thermo') or pierwsza.get('thermo'),
                       pierwsza['details'].get('ingredients'), pierwsza['details'].get('allergens'), tylko_puste=not nadpisz)
        najlepsza = max(obserwacje, key=lambda x: x[5].get('reviewsNumber') or 0)[5]
        n = najlepsza.get('reviewsNumber') or 0
        if n:
            s = liczba(najlepsza.get('reviewsScore'))
            z = d.get('ocena_z_dnia') or ''
            nowsza = (dzien >= z) if self.nowsze else d.get('liczba_ocen') is None
            if nowsza and (s, n) != (d.get('ocena_proc'), d.get('liczba_ocen')):
                d['ocena_proc'], d['liczba_ocen'], d['ocena_z_dnia'] = s, n, dzien

    # -------- wejścia

    def scal_okno(self, okno, dzien, z_tierami, pobrano):
        grupy = defaultdict(list)
        obserwacje = defaultdict(list)  # id dania → [(zrodlo, priorytet, data, kcal, dieta, opcja)]
        for dm in okno.get('days', []):
            if dm.get('dietId') in z_tierami and not dm.get('tier'):
                continue  # example-menu diety z tierami — pełniej pokrywają ją tiery (menu-configuration)
            realne = 0
            for meal in dm.get('meals', []):
                for o in meal.get('options', []):
                    if not realna_opcja(o):
                        continue
                    realne += 1
                    d = self.danie(o['name'], dzien)
                    zrodlo = (o.get('label') or dm['dietName']).strip()
                    dodaj(d['pory'], meal.get('name'))
                    dodaj(d['diety'], dm['dietName'])
                    dodaj(d['etykiety'], zrodlo)
                    obserwacje[d['id']].append((zrodlo, 0 if dm.get('tier') else 1, dm['date'], dm.get('calories'),
                                                dm['dietName'], o))
            if realne:
                grupy[(dm['date'], dm['dietName'])].append(dm)
                self.stat['pozycji'] += realne
        po_id = {d['id']: d for d in self.dania}
        for i, obs in obserwacje.items():
            self._zastosuj(po_id[i], obs, dzien)
        for (data, dieta), warianty in grupy.items():
            ref = next((x for x in warianty if x.get('calories') == REF_KCAL), None) or \
                min(warianty, key=lambda x: abs((x.get('calories') or 0) - REF_KCAL))
            nowe = {}
            for meal in ref.get('meals', []):
                for o in meal.get('options', []):
                    if not realna_opcja(o):
                        continue
                    d = self.po_kluczu[klucz(o['name'])]
                    kk = (meal.get('name'), d['id'])
                    if kk in nowe:
                        continue
                    mak = makro_z(o['details'])
                    nowe[kk] = dict(zip(MENU_KOL, [
                        data, dieta, ref.get('dietId'), ref.get('calories'), (o.get('label') or dieta).strip(),
                        meal.get('name'), d['id'], d['danie'], *[mak[k] for k, _ in MAKRO],
                        TERMIKA.get(o['details'].get('thermo') or o.get('thermo'), ''), pobrano]))
            stare_wiersze = self.menu.get((data, dieta), [])
            if stare_wiersze:
                if not self.nowsze:
                    continue  # starszy plik nie nadpisuje nowszego menu
                stary_kcal = na_liczbe(stare_wiersze[0].get('kcal_diety')) or 0
                if abs((ref.get('calories') or 0) - REF_KCAL) > abs(stary_kcal - REF_KCAL):
                    continue  # plik bez wariantu referencyjnego nie psuje lepszego menu
            if sorted(map(tresc_wiersza, stare_wiersze)) == sorted(map(tresc_wiersza, nowe.values())):
                continue  # to samo menu — zostaw (łącznie z datą pobrania), żeby nie mieszać w historii git
            stare = {(r['pora'], int(r['danie_id'])) for r in stare_wiersze}
            self.stat['menu_nowe'] += len(set(nowe) - stare)
            self.stat['menu_usuniete'] += len(stare - set(nowe))
            bez_zmian = {tresc_wiersza(r): r.get('pobrano') for r in stare_wiersze}
            for r in nowe.values():  # niezmienione wiersze zachowują datę pobrania → w git widać tylko podmienione dania
                r['pobrano'] = bez_zmian.get(tresc_wiersza(r)) or r['pobrano']
            self.menu[(data, dieta)] = list(nowe.values())
        self.stat['dni_menu'] = len({k[0] for k in grupy})
        if grupy:
            self.stat['menu_od'] = min(k[0] for k in grupy)
            self.stat['menu_do'] = max(k[0] for k in grupy)
        self.stat['bledy_pobierania'] += len(okno.get('errors') or [])

    def scal_dodatki(self, elementy, dzien):
        po_id = {x['id']: x for x in self.dodatki}
        obecne = set()
        for e in elementy or []:
            i = (e.get('id') or {}).get('possibleSideOrderId')
            if i is None:
                continue
            obecne.add(i)
            opis = (e.get('subtitle') or '').strip()
            w = wartosci_dodatku(opis)
            nowy = {'id': i, 'nazwa': ' '.join((e.get('title') or '').split()), 'cena': liczba(e.get('defaultPrice')),
                    'kategoria': e.get('category') or '', 'kcal': w['kcal'], 'bialko': w['bialko'],
                    'tluszcz': w['tluszcz'], 'wegle': w['wegle'], 'ocena': liczba(e.get('rateScore')),
                    'liczba_ocen': e.get('rateNumber'), 'obraz': e.get('imageUrl') or '', 'opis': opis,
                    'dostepny': True, 'dodano': dzien}
            stary = po_id.get(i)
            if stary is None:
                if not self.nowsze:
                    nowy['dostepny'] = False
                self.dodatki.append(nowy)
                po_id[i] = nowy
                self.stat['nowe_dodatki'] += 1
            elif self.nowsze:
                nowy['dodano'] = stary.get('dodano') or dzien
                stary.clear()
                stary.update(nowy)
        if obecne and self.nowsze:
            for x in self.dodatki:
                if x['id'] not in obecne:
                    x['dostepny'] = False

    def scal_diety(self, szczegoly, diety_firmy=None):
        if self.diety and not self.nowsze:
            return
        stare = {d['dietId']: d for d in self.diety}
        firma = {c.get('dietId'): c for c in (diety_firmy or [])}
        wynik = []
        for p in szczegoly.get('programs', []):
            for d in p.get('diets', []):
                warianty = []
                for o in d.get('dietOptions') or []:
                    for k in o.get('dietCalories') or []:
                        warianty.append(f"{o['name'].strip()}: {k['calories']} kcal ({k.get('price', '')})")
                for t in d.get('dietTiers') or []:
                    for o in t.get('dietOptions') or []:
                        for k in o.get('dietCalories') or []:
                            warianty.append(f"{t['name'].strip()} / {o['name'].strip()}: {k['calories']} kcal ({k.get('price', '')})")
                c = firma.get(d['dietId']) or {}
                opis = oczysc(d.get('description') or c.get('description') or c.get('dietDescription')) or \
                    (stare.get(d['dietId']) or {}).get('opis') or ''
                wynik.append({'dietId': d['dietId'], 'dieta': d['name'].strip(), 'program': p.get('programName'),
                              'tag': d.get('dietTag'), 'dan_dziennie': d.get('dietMealCount'),
                              'cena_min': d.get('minDietPrice'), 'ocena': d.get('feedbackValue'),
                              'opinii': d.get('feedbackNumber'),
                              'tiery': [t['name'].strip() for t in d.get('dietTiers') or []],
                              'warianty': warianty, 'opis': opis})
        if wynik:
            self.diety = wynik

    # -------- serializacja

    def _dania_lista(self):
        return [{k: d.get(k) for k in POLA_DANIA} for d in sorted(self.dania, key=lambda d: d['id'])]

    def _menu_wiersze(self):
        wiersze = [r for rows in self.menu.values() for r in rows]
        return sorted(wiersze, key=lambda r: (r['data'], r['dieta'], pora_idx(r['pora']), str(r['etykieta']), str(r['danie'])))

    def _przelicz(self):
        w_menu = defaultdict(set)
        for rows in self.menu.values():
            for r in rows:
                w_menu[int(r['danie_id'])].add(r['data'])
        for d in self.dania:
            d['pory'] = sorted(set(d['pory']), key=pora_idx)
            d['diety'] = sorted(set(d['diety']))
            d['etykiety'] = sorted(set(x for x in d['etykiety'] if x))
            ustal_makro(d)
            d['w_menu_daty'] = sorted(w_menu.get(d['id'], []))

    def pliki(self):
        """Wszystkie pliki wynikowe jako {ścieżka: treść}."""
        self._przelicz()
        dania = self._dania_lista()
        menu = self._menu_wiersze()
        dodatki = sorted(self.dodatki, key=lambda x: x['id'])
        out = {
            sciezka('dania.json'): json_linie(dania),
            sciezka('menu.csv'): csv_tekst(MENU_KOL, menu),
            sciezka('dodatki.json'): json_linie(dodatki),
            sciezka('diety.json'): json_linie(self.diety),
        }
        # eksporty dla Excela
        kol = ['id', 'danie', 'pory', 'diety', 'etykiety'] + MAKRO_KOL + \
              ['makro_dla_wariantu_kcal', 'termika', 'skladniki', 'skladniki_glowne', 'alergeny', 'ocena_proc',
               'liczba_ocen', 'dodano', 'w_menu_dni', 'w_menu_od', 'w_menu_do']
        wiersze = []
        for d in dania:
            m = d['makro']
            wiersze.append(dict(zip(kol, [
                d['id'], d['danie'], ' | '.join(d['pory']), ' | '.join(d['diety']), ' | '.join(d['etykiety']),
                *[m.get(k) for k, _ in MAKRO], d['makro_kcal'], d['termika'], ' | '.join(d['skladniki']),
                ' | '.join(d['skladniki_glowne']), ' | '.join(d['alergeny']), d['ocena_proc'], d['liczba_ocen'],
                d['dodano'], len(d['w_menu_daty']), d['w_menu_daty'][0] if d['w_menu_daty'] else None,
                d['w_menu_daty'][-1] if d['w_menu_daty'] else None])))
        out[sciezka('dania.csv')] = csv_tekst(kol, wiersze)
        out[sciezka('dodatki.csv')] = csv_tekst(
            ['id', 'nazwa', 'cena', 'kategoria', 'kcal', 'bialko', 'tluszcz', 'wegle', 'ocena', 'liczba_ocen', 'dostepny',
             'dodano', 'obraz', 'opis'],
            [{**x, 'dostepny': 'tak' if x.get('dostepny') else 'nie', 'opis': re.sub(r'\s*\n\s*', ' | ', x.get('opis') or '')}
             for x in dodatki])
        out[sciezka('diety.csv')] = csv_tekst(
            ['dietId', 'dieta', 'program', 'tag', 'dan_dziennie', 'cena_min', 'ocena', 'opinii', 'tiery', 'warianty', 'opis'],
            [{**x, 'tiery': ' | '.join(x.get('tiery') or []), 'warianty': ' | '.join(x.get('warianty') or [])} for x in self.diety])
        return out

    def odcisk(self):
        h = hashlib.sha1()
        for p, t in sorted(self.pliki().items()):
            if not p.endswith('.csv') or p.endswith('menu.csv'):
                h.update(t.encode('utf-8'))
        return h.hexdigest()

    def zapisz_wszystko(self):
        zmienione = []
        pliki = self.pliki()
        pliki[sciezka('log.csv')] = csv_tekst(LOG_KOL, self.log)
        daty = sorted({r['data'] for rows in self.menu.values() for r in rows})
        self.meta.update({'dan': len(self.dania), 'dodatkow': len(self.dodatki), 'dni_menu': len(daty),
                          'menu_od': daty[0] if daty else None, 'menu_do': daty[-1] if daty else None})
        pliki[sciezka('meta.json')] = json.dumps(self.meta, ensure_ascii=False, indent=1) + '\n'
        pliki[os.path.join(ROOT, 'dane.js')] = self.dane_js()
        for p, t in pliki.items():
            if zapisz(p, t):
                zmienione.append(os.path.relpath(p, ROOT))
        return zmienione

    def dane_js(self):
        """Dane dla przegladarka.html (plik JS, bo przeglądarka nie wczyta JSON-a z dysku przez fetch)."""
        dania = [danie_js(d) for d in self._dania_lista()]

        def w_bazie(nazwa):
            return sum(1 for d in dania if any(x == nazwa or x.startswith(nazwa + ' / ') for x in d['diety']))
        diety = [{**x, 'dan_w_bazie': w_bazie(x['dieta'])} for x in self.diety]
        ocena = {d['id']: (d['ocena_proc'], d['liczba_ocen']) for d in dania}
        menu = [[r['data'], r['dieta'], na_liczbe(r['dietId']), na_liczbe(r['kcal_diety']), r['etykieta'], r['pora'],
                 na_liczbe(r['danie_id']), na_liczbe(r['kcal']), na_liczbe(r['bialko_g']), na_liczbe(r['tluszcz_g']),
                 na_liczbe(r['wegle_g']), *ocena.get(na_liczbe(r['danie_id']), (None, None))] for r in self._menu_wiersze()]
        czesci = {'aktualizacja': self.meta.get('aktualizacja'), 'dania': dania, 'diety': diety,
                  'dodatki': sorted(self.dodatki, key=lambda x: x['id']), 'menu': menu}
        return plik_js('window.DANE', czesci)


def plik_js(zmienna, czesci):
    linie = []
    for k, v in czesci.items():
        if isinstance(v, list) and v:
            body = ',\n'.join(json.dumps(x, ensure_ascii=False, separators=(',', ':')) for x in v)
            linie.append(f'{json.dumps(k)}:[\n{body}\n]')
        else:
            linie.append(f'{json.dumps(k)}:{json.dumps(v, ensure_ascii=False)}')
    return f'// wygenerowane przez scripts/scal.py — nie edytuj ręcznie\n{zmienna} = {{\n' + ',\n'.join(linie) + '\n};\n'


# ---------------------------------------------------------------- prywatne: historia zamówień

class Prywatne:
    """Historia zamówień z panelu klienta — tylko w prywatne/ (poza gitem).
    Publiczną bazę wyłącznie czyta: dania łączy z nią po kluczu nazwy, a danie, którego w chwili importu nie ma
    w bazie, trafia do prywatne/dania.json. Dla przegladarka.html powstaje prywatne/dane.js (window.DANE_PRYWATNE)."""

    def __init__(self):
        self.historia = wczytaj_csv(self.sciezka('historia.csv'))
        self.zamowienia = wczytaj_json(self.sciezka('zamowienia.json'), [])
        self.dania = wczytaj_json(self.sciezka('dania.json'), [])  # tylko dania spoza publicznej bazy
        self.meta = wczytaj_json(self.sciezka('meta.json'), {})

    @staticmethod
    def sciezka(nazwa):
        return os.path.join(PRYW, nazwa)

    def aktywne(self):
        return bool(self.historia or self.zamowienia or self.dania)

    def _danie(self, nazwa, dzien, baza):
        """Rekord prywatnego dania albo None, gdy danie jest w publicznej bazie (historia go nie zmienia)."""
        k = klucz(nazwa)
        if k in baza.po_kluczu:
            return None
        d = next((x for x in self.dania if x['klucz'] == k), None)
        if d is None:
            d = {'id': max((x['id'] for x in self.dania), default=0) + 1, 'danie': ' '.join(nazwa.split()), 'klucz': k,
                 'pory': [], 'diety': [], 'etykiety': [], 'warianty': {}, 'waga_g': None, 'termika': '',
                 'skladniki': [], 'skladniki_glowne': [], 'alergeny': [], 'dodano': dzien}
            self.dania.append(d)
        return d

    def scal_historie(self, hist, dzien, baza):
        zam = {o['orderId']: o for o in hist.get('orders', [])}
        wiersze = []
        for m in sorted(hist.get('deliveryMenus', []), key=lambda m: m['date']):
            o = zam.get(m['orderId'], {})
            dieta = o.get('diet') or {}
            posilki = [x for x in (m.get('menu') or {}).get('deliveryMenuMeal', []) if not x.get('deleted')]
            for x in sorted(posilki, key=lambda x: x.get('mealPriority') or 0):
                if not x.get('menuMealName') or ZASLEPKA.match(x['menuMealName']):
                    continue
                nu = x.get('nutrition') or {}
                mak = makro_z(nu)
                d = self._danie(x['menuMealName'], dzien, baza)
                if d is not None:
                    dodaj(d['pory'], x.get('mealName'))
                    dodaj(d['diety'], dieta.get('dietName') or 'Wybór menu')
                    dodaj(d['etykiety'], (x.get('dietName') or '').strip())
                    if mak['kcal'] is not None and dieta.get('calories'):
                        d['warianty'].setdefault(str(dieta['calories']), mak)
                    if nu.get('weight'):
                        d['waga_g'] = liczba(nu['weight'])
                    Baza._opis(d, x.get('thermo'), x.get('ingredients'), x.get('allergens'), tylko_puste=True)
                nazwa = (d or baza.po_kluczu[klucz(x['menuMealName'])])['danie']
                wiersze.append(dict(zip(HIST_KOL, [
                    m['date'], m['orderId'], dieta.get('programName'), dieta.get('dietOptionName') or dieta.get('tierName'),
                    dieta.get('calories'), dieta.get('mealsNumber'), x.get('mealName'), None, nazwa,
                    x.get('dietName'), x.get('amount'), *[mak[k] for k, _ in MAKRO], liczba(nu.get('weight')),
                    TERMIKA.get(x.get('thermo'), x.get('thermo') or '')])))
        nowe = [{'id': o['orderId'], 'od': o.get('dateFrom'), 'do': o.get('dateTo'),
                 'program': (o.get('diet') or {}).get('programName'),
                 'wariant': (o.get('diet') or {}).get('dietOptionName') or (o.get('diet') or {}).get('tierName'),
                 'kcal': (o.get('diet') or {}).get('calories'), 'posilkow': (o.get('diet') or {}).get('mealsNumber'),
                 'dostaw': sum(1 for x in o.get('deliveries', []) if not x.get('deleted')),
                 'koszt': liczba((o.get('payment') or {}).get('cost'))} for o in zam.values()]
        # scalanie po zamówieniu: zamówienia z pliku zastępują swoje stare wiersze, pozostałe zostają
        # (prywatne/ nie ma kopii w git, więc częściowy plik nie może skasować wcześniejszej historii)
        w_pliku = {z['id'] for z in nowe} | {na_liczbe(r['zamowienie']) for r in wiersze}
        self.historia = [r for r in self.historia if na_liczbe(r['zamowienie']) not in w_pliku] + wiersze
        self.zamowienia = sorted([z for z in self.zamowienia if z['id'] not in w_pliku] + nowe,
                                 key=lambda z: (z['od'] or '', z['id']))

    def zapisz_wszystko(self, baza):
        """Id dań w historii są ustalane przy każdym zapisie (po kluczu nazwy), więc danie, które później
        pojawi się w publicznym menu, samo przechodzi z prywatnego id na publiczne."""
        pryw = {d['klucz']: d for d in self.dania if d['klucz'] not in baza.po_kluczu}
        for r in self.historia:
            k = klucz(r['danie'])
            r['danie_id'] = baza.po_kluczu[k]['id'] if k in baza.po_kluczu else \
                PRYW_ID + pryw[k]['id'] if k in pryw else None
        hist = sorted(self.historia, key=lambda r: (r['data'], pora_idx(r['pora']), str(r['danie'])))
        for d in self.dania:
            d['pory'] = sorted(set(d['pory']), key=pora_idx)
            d['diety'] = sorted(set(d['diety']))
            d['etykiety'] = sorted(set(x for x in d['etykiety'] if x))
            ustal_makro(d)
        js = {'historia': [[r['data'], na_liczbe(r['zamowienie']), r['wariant_diety'], na_liczbe(r['kcal_diety']), r['pora'],
                            na_liczbe(r['danie_id']), na_liczbe(r['kcal']), na_liczbe(r['bialko_g']),
                            na_liczbe(r['tluszcz_g']), na_liczbe(r['wegle_g']), na_liczbe(r['waga_g'])] for r in hist],
              'zamowienia': self.zamowienia,
              'dania': [danie_js({**d, 'id': PRYW_ID + d['id']}, waga_g=d.get('waga_g'))
                        for d in sorted(pryw.values(), key=lambda d: d['id'])]}
        pliki = {
            self.sciezka('historia.csv'): csv_tekst(HIST_KOL, hist),
            self.sciezka('zamowienia.json'): json_linie(self.zamowienia),
            self.sciezka('dania.json'): json_linie(sorted(self.dania, key=lambda d: d['id'])),
            self.sciezka('meta.json'): json.dumps(self.meta, ensure_ascii=False, indent=1) + '\n',
            self.sciezka('dane.js'): plik_js('window.DANE_PRYWATNE', js),
        }
        zmienione = [os.path.relpath(p, ROOT) for p, t in pliki.items() if zapisz(p, t)]
        bez_id = sum(1 for r in self.historia if r['danie_id'] is None)
        print(f"prywatne/: {len(self.zamowienia)} zamówień, {len(hist)} posiłków, {len(pryw)} dań spoza publicznej bazy"
              + (f", {bez_id} posiłków bez dania w żadnej bazie" if bez_id else '')
              + '; zmienione: ' + (', '.join(zmienione) or 'brak'))


# ---------------------------------------------------------------- główna

PUBLICZNE = ('window', 'sideOrders', 'dietDetails')


def main(argv=None):
    pliki = sys.argv[1:] if argv is None else list(argv)
    baza = Baza()
    wejscia, historie = [], []
    for p in pliki:
        surowe = wczytaj_surowe(p)
        fetched = surowe.get('fetchedAt') or ''
        if any(surowe.get(k) for k in PUBLICZNE):
            wejscia.append((fetched, p, surowe))
        if surowe.get('history'):
            historie.append((fetched, p, surowe['history']))
    wejscia.sort(key=lambda x: (x[0], x[1]))
    start = baza.odcisk() if wejscia else None
    ostatnia = baza.meta.get('aktualizacja') or ''
    podsumowanie, zakres, wpisy = Counter(), [], []
    for fetched, p, surowe in wejscia:
        dzien = fetched[:10] or dt.date.today().isoformat()
        baza.stat = Counter()
        baza.nowsze = not ostatnia or fetched >= ostatnia
        z_tierami = {d['dietId'] for pr in (surowe.get('dietDetails') or {}).get('programs', [])
                     for d in pr.get('diets', []) if d.get('dietTiers')}
        if surowe.get('window'):
            baza.scal_okno(surowe['window'], dzien, z_tierami, fetched)
        if surowe.get('sideOrders'):
            baza.scal_dodatki(surowe['sideOrders'], dzien)
        if surowe.get('dietDetails'):
            baza.scal_diety(surowe['dietDetails'], surowe.get('companyDiets'))
        if baza.nowsze:
            ostatnia = fetched
        s = baza.stat
        print(f"{os.path.basename(p)}{'' if baza.nowsze else ' (starszy — tylko uzupełnianie)'}: "
              f"menu {s.get('menu_od', '-')}..{s.get('menu_do', '-')}, pozycji {s['pozycji']}, nowe dania {s['nowe_dania']}, "
              f"wiersze menu +{s['menu_nowe']}/-{s['menu_usuniete']}, nowe dodatki {s['nowe_dodatki']}")
        wpisy.append({'pobrano': fetched, 'plik': os.path.basename(p), 'dni_menu': s['dni_menu'],
                      'menu_od': s.get('menu_od'), 'menu_do': s.get('menu_do'), 'pozycji': s['pozycji'],
                      'nowe_dania': s['nowe_dania'], 'menu_nowe': s['menu_nowe'], 'menu_usuniete': s['menu_usuniete'],
                      'nowe_dodatki': s['nowe_dodatki'], 'bledy_pobierania': s['bledy_pobierania'],
                      'dan_w_bazie': len(baza.dania)})
        for k in ('nowe_dania', 'nowe_dodatki', 'menu_nowe', 'menu_usuniete', 'bledy_pobierania'):
            podsumowanie[k] += s[k]
        if s.get('menu_od'):
            zakres += [s['menu_od'], s['menu_do']]
    zmiana = bool(wejscia) and baza.odcisk() != start
    if zmiana:
        baza.meta['aktualizacja'] = max([baza.meta.get('aktualizacja') or ''] + [w[0] for w in wejscia])
        baza.log.extend(wpisy)
    zmienione = baza.zapisz_wszystko()
    pryw = Prywatne()
    for fetched, p, hist in sorted(historie, key=lambda x: (x[0], x[1])):
        if fetched >= (pryw.meta.get('historia_z') or ''):
            pryw.scal_historie(hist, fetched[:10] or dt.date.today().isoformat(), baza)
            pryw.meta['historia_z'] = fetched
            print(f'{os.path.basename(p)}: historia zamówień → prywatne/')
    if pryw.aktywne():
        pryw.zapisz_wszystko(baza)
    okno = f'{min(zakres)}→{max(zakres)}' if zakres else '(brak menu w pliku)'
    if not wejscia:
        tytul = f"przebudowano eksporty z data/ ({len(baza.dania)} dań w bazie)"
    elif not zmiana:
        tytul = f"menu {okno}: bez zmian ({len(baza.dania)} dań w bazie)"
    elif podsumowanie['nowe_dania'] or podsumowanie['nowe_dodatki']:
        tytul = (f"menu {okno}: +{podsumowanie['nowe_dania']} dań, +{podsumowanie['nowe_dodatki']} dodatków "
                 f"({len(baza.dania)} dań w bazie)")
    else:
        tytul = f"menu {okno}: bez nowych dań, aktualizacja menu i ocen ({len(baza.dania)} dań w bazie)"
    print(tytul)
    print('zmienione pliki: ' + (', '.join(zmienione) if zmienione else 'brak'))
    if pliki:
        os.makedirs(RAW, exist_ok=True)
        with open(os.path.join(RAW, 'commit_msg.txt'), 'w', encoding='utf-8') as f:
            f.write(tytul + '\n\n' + '\n'.join(f'- {k}: {v}' for k, v in podsumowanie.items()) + '\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(f"### {tytul}\n\n| | |\n|---|---|\n" +
                    ''.join(f'| {k} | {v} |\n' for k, v in podsumowanie.items()) +
                    f"| zmienione pliki | {', '.join(zmienione) or 'brak'} |\n")
    return 0


if __name__ == '__main__':
    sys.exit(main())
