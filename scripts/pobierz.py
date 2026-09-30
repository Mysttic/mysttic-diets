#!/usr/bin/env python3
"""Pobiera bieżące menu Kuchni Vikinga z publicznego API (bez logowania) i scala je z bazą w data/.

  python scripts/pobierz.py                  pobierz → raw/menu_<czas>.json.gz → scripts/scal.py → eksporty
  python scripts/pobierz.py --bez-scalania   tylko pobierz

Pobierane endpointy (publiczne; menu jest takie samo w całej Polsce):
  order-form/diet-details                     diety, tiery Wyboru menu, kaloryczności, ceny
  order-form/steps/example-menu               menu diet gotowych (Standard, Light, Keto…), dzień po dniu
  order-form/steps/menu-configuration/meals   menu każdego tieru Wyboru menu w każdej kaloryczności
  order-form/steps/side-orders                dodatki
Menu jest publikowane ok. 2 tygodnie do przodu, więc regularne uruchamianie buduje archiwum.
Kod wyjścia 2 = nie pobrano żadnego menu (API niedostępne albo zmienione).
"""
import argparse
import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scal  # noqa: E402

API = 'https://panel.kuchniavikinga.pl/api/panel/open'
MIASTO = 918123  # Warszawa (menu jest takie samo w całej Polsce — miasto dowolne)
UA = 'Mozilla/5.0 (compatible; mysttic-diets/1.0; archiwum menu)'


def pobierz_json(sciezka, **parametry):
    url = f'{API}/{sciezka}?{urllib.parse.urlencode(parametry)}'
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    blad = None
    for proba in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            blad = e
            if 400 <= e.code < 500 and e.code != 429:
                break  # błąd zapytania — ponawianie nic nie da
        except Exception as e:  # sieć, timeout, zły JSON
            blad = e
        time.sleep(2 * (proba + 1))
    raise blad


def realne(j):
    return sum(1 for m in (j or {}).get('meals', []) for o in m.get('options', []) if scal.realna_opcja(o))


def zakres(od, do):
    return [(od + dt.timedelta(days=i)).isoformat() for i in range((do - od).days + 1)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dni', type=int, default=21, help='ile dni do przodu sprawdzać menu diet gotowych (domyślnie 21)')
    ap.add_argument('--od', type=dt.date.fromisoformat, default=None, help='pierwszy dzień (domyślnie dziś)')
    ap.add_argument('--watki', type=int, default=4, help='równoległe zapytania (domyślnie 4)')
    ap.add_argument('--bez-scalania', action='store_true', help='tylko zapisz surowe dane w raw/')
    a = ap.parse_args(argv)

    teraz = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    od = a.od or teraz.date()
    try:
        szczegoly = pobierz_json('order-form/diet-details', cityId=MIASTO)
    except Exception as e:
        print(f'BŁĄD: nie udało się pobrać listy diet: {e}', file=sys.stderr)
        return 2

    diety, tiery = [], []
    for p in szczegoly.get('programs', []):
        for d in p.get('diets', []):
            if not d.get('dietTiers'):
                diety.append({'dietId': d['dietId'], 'dietName': d['name'].strip()})
                continue
            for t in d['dietTiers']:  # każdy tier ma własny zestaw dań, każda kaloryczność własne makro
                widziane = set()
                for o in t.get('dietOptions') or []:
                    for k in o.get('dietCalories') or []:
                        if k.get('calories') in widziane:
                            continue
                        widziane.add(k.get('calories'))
                        tiery.append({'dietId': d['dietId'], 'dietName': f"{d['name'].strip()} / {t['name'].strip()}",
                                      'tier': t['name'].strip(), 'calories': k.get('calories'),
                                      'dietCaloriesId': k['dietCaloriesId'],
                                      'mealIds': [m['dietCaloriesMealId'] for m in k.get('dietCaloriesMealIds') or []]})

    daty = zakres(od, od + dt.timedelta(days=a.dni))
    daty_tierow = daty
    if tiery:
        try:
            s = pobierz_json('order-form/steps/menu-configuration/settings',
                             dietCaloriesId=tiery[0]['dietCaloriesId'], cityId=MIASTO)
            daty_tierow = zakres(dt.date.fromisoformat(s['configurationPossibleFrom']),
                                 dt.date.fromisoformat(s['configurationPossibleTo']))
        except Exception as e:
            print(f'uwaga: nie znam okna konfiguracji menu ({e}) — sprawdzam {daty[0]}..{daty[-1]}')

    zadania = [('example-menu', d, data) for d in diety for data in daty] + \
              [('menu-configuration', t, data) for t in tiery for data in daty_tierow]
    bledy = []

    def wykonaj(z):
        rodzaj, x, data = z
        try:
            if rodzaj == 'example-menu':
                j = pobierz_json('order-form/steps/example-menu', dietId=x['dietId'], cityId=MIASTO, date=data)
                meta = {'dietId': x['dietId'], 'dietName': x['dietName']}
            else:
                j = pobierz_json('order-form/steps/menu-configuration/meals', dietCaloriesId=x['dietCaloriesId'],
                                 cityId=MIASTO, date=data, dietCaloriesMealIds=','.join(map(str, x['mealIds'])))
                meta = {k: x[k] for k in ('dietId', 'dietName', 'tier', 'dietCaloriesId')}
            if realne(j):
                return {**meta, 'date': data, 'calories': j.get('calories') or x.get('calories'), 'meals': j.get('meals', [])}
        except Exception as e:
            bledy.append({'rodzaj': rodzaj, 'dieta': x['dietName'], 'data': data, 'blad': str(e)[:300]})
        return None

    with ThreadPoolExecutor(max_workers=max(1, a.watki)) as ex:
        dni = [r for r in ex.map(wykonaj, zadania) if r]
    try:
        dodatki = (pobierz_json('order-form/steps/side-orders', cityId=MIASTO) or {}).get('elements') or []
    except Exception as e:
        dodatki = []
        bledy.append({'rodzaj': 'side-orders', 'dieta': '', 'data': '', 'blad': str(e)[:300]})

    dni.sort(key=lambda x: (x['date'], x['dietName'], x.get('calories') or 0))
    snapshot = {'fetchedAt': teraz.isoformat().replace('+00:00', 'Z'), 'cityId': MIASTO, 'zrodlo': API,
                'window': {'diets': diety, 'tiers': tiery, 'dates': sorted({x['date'] for x in dni}), 'days': dni,
                           'errors': bledy},
                'dietDetails': szczegoly, 'sideOrders': dodatki}
    os.makedirs(scal.RAW, exist_ok=True)
    plik = os.path.join(scal.RAW, f"menu_{teraz.strftime('%Y-%m-%dT%H%MZ')}.json.gz")
    with gzip.open(plik, 'wt', encoding='utf-8') as f:
        json.dump(snapshot, f, ensure_ascii=False)
    pozycji = sum(realne(x) for x in dni)
    print(f"pobrano: {len(zadania)} zapytań, {len(snapshot['window']['dates'])} dni z menu, {pozycji} pozycji, "
          f"{len(dodatki)} dodatków, błędów {len(bledy)} → {os.path.relpath(plik, scal.ROOT)}")
    for b in bledy[:10]:
        print(f"  błąd: {b['rodzaj']} {b['dieta']} {b['data']}: {b['blad']}")
    if not dni:
        print('BŁĄD: nie pobrano żadnego menu — API niedostępne albo zmieniło format', file=sys.stderr)
        return 2
    if a.bez_scalania:
        return 0
    return scal.main([plik])


if __name__ == '__main__':
    sys.exit(main())
