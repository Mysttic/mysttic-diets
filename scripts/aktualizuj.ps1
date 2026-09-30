# Lokalna alternatywa dla GitHub Actions (np. z Harmonogramu zadań Windows):
# pobiera aktualne menu, scala je z bazą bez dubli, commituje i wypycha zmiany.
Set-Location (Split-Path $PSScriptRoot -Parent)
git pull --rebase --autostash
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python scripts/pobierz.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
git add data dane.js
$prywatne = git diff --cached --name-only | Select-String -Pattern 'historia\.csv|zamowienia\.json|prywatne'
if ($prywatne) { Write-Error "Historia zamówień nie może trafić do repo: $prywatne"; exit 1 }
python scripts/strona.py | Out-Null  # przerywa, gdy dane.js ma ślady historii
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) { Write-Host 'Brak zmian w danych.'; exit 0 }
git commit -q -F raw/commit_msg.txt
$wypychane = git diff --name-only origin/main..HEAD | Select-String -Pattern 'historia\.csv|zamowienia\.json|prywatne'
if ($wypychane) { Write-Error "Historia zamówień w commitach do wypchnięcia: $wypychane"; exit 1 }
git push
