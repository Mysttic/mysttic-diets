#!/usr/bin/env python3
"""Buduje stronę dla GitHub Pages → _site/ (index.html + dane.js).

dane.js jest publiczny z konstrukcji (scal.py nie zapisuje do niego historii zamówień). Skrypt dodatkowo:
  • usuwa z index.html odwołanie do prywatne/dane.js (na Pages go nie ma),
  • dokleja noindex,
  • przerywa build, jeśli w dane.js są ślady historii zamówień — tania asekuracja przed regresją.

Użycie: python scripts/strona.py [katalog]   (domyślnie _site)
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIKS = 'window.DANE = '
NOINDEX = '<meta name="robots" content="noindex, nofollow">'
PRYWATNE = '<script src="prywatne/dane.js"></script>\n'
ZAKAZANE_KLUCZE = {'historia', 'zamowienia'}
ZAKAZANE_POLA_DANIA = {'jadlem_daty', 'zrodlo', 'waga_g'}


def sprawdz(D):
    bledy = [f'klucz „{k}”' for k in ZAKAZANE_KLUCZE & D.keys()]
    for d in D.get('dania', []):
        pola = ZAKAZANE_POLA_DANIA & d.keys()
        if pola:
            bledy.append(f"danie {d.get('id')}: {', '.join(sorted(pola))}")
            break
    if bledy:
        sys.exit('BŁĄD: dane.js zawiera dane z historii zamówień: ' + '; '.join(bledy))


def main():
    out = os.path.join(ROOT, sys.argv[1] if len(sys.argv) > 1 else '_site')
    with open(os.path.join(ROOT, 'dane.js'), encoding='utf-8') as f:
        dane = f.read()
    sprawdz(json.loads(dane[dane.index(PREFIKS) + len(PREFIKS):].rstrip().rstrip(';')))

    with open(os.path.join(ROOT, 'przegladarka.html'), encoding='utf-8') as f:
        html = f.read()
    for stare, nowe in (('<meta charset="utf-8">', '<meta charset="utf-8">\n' + NOINDEX), (PRYWATNE, '')):
        if html.count(stare) != 1:
            sys.exit(f'BŁĄD: w przegladarka.html brak {stare.strip()}')
        html = html.replace(stare, nowe)

    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'index.html'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(html)
    with open(os.path.join(out, 'dane.js'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(dane)
    print(f'{out}: index.html + dane.js')


if __name__ == '__main__':
    main()
