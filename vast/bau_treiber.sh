#!/bin/bash
# bau_treiber.sh <endpunkt> [projektverzeichnis]
#
# Derselbe Bauauftrag fuer jedes Modell, egal ob es auf der CPU des Dell
# rechnet oder auf einer gemieteten GPU. Nur die Adresse ist verschieden -
# damit sind die Ergebnisse vergleichbar.
#
# In Stufen, weil kicode je Auftrag 24 Runden hat: eine Sitzung je Stufe, das
# Gedaechtnis dazwischen sind die Dateien im Projektverzeichnis.
#
# Jede Stufe nennt die Datei, die danach dasein muss. Fehlt sie, wird die
# Stufe wiederholt. Am 29.08.2026 brach Stufe 1 nach einem HTTP 500 des
# llama-servers ab, ohne eine Zeile geschrieben zu haben - die folgenden
# Stufen bauten dann auf einer Datei auf, die es nicht gab.
set -u
E="${1:-http://127.0.0.1:8082}"
P="${2:-/home/gh/vast_optimizer_miet}"
LOG="$P/bau.log"
VERSUCHE=3
mkdir -p "$P"
cp -n /home/gh/vast_optimizer/AUFTRAG.md "$P/" 2>/dev/null
cp -n /home/gh/gpu_mieten.py "$P/referenz_gpu_mieten.py" 2>/dev/null
KI="python3 -u /home/gh/kicode.py --endpunkt $E --api-key-datei /home/gh/.config/llm_fern/api_key --projekt $P --frei"

# stufe <name> <ergebnisdatei|-> <auftrag>
stufe() {
    local name="$1" ziel="$2" auftrag="$3" v
    for v in $(seq 1 $VERSUCHE); do
        echo "" >> "$LOG"
        echo "########## $(date "+%F %T")  Stufe $name  Versuch $v  ($E)" >> "$LOG"
        timeout 7200 $KI "$auftrag" >> "$LOG" 2>&1
        local rc=$?
        echo "########## $(date "+%F %T")  Stufe $name beendet (rc=$rc)" >> "$LOG"
        if [ "$ziel" = "-" ] || [ -s "$P/$ziel" ]; then
            return 0
        fi
        echo "!!!!!!!!!! $(date "+%F %T")  $ziel fehlt - Stufe $name wird wiederholt" >> "$LOG"
    done
    echo "!!!!!!!!!! $(date "+%F %T")  Stufe $name aufgegeben nach $VERSUCHE Versuchen" >> "$LOG"
    return 1
}

echo "===== Bau begonnen $(date "+%F %T") gegen $E =====" >> "$LOG"

stufe 1 vast_optimizer.py "Lies AUFTRAG.md und referenz_gpu_mieten.py in diesem Verzeichnis vollstaendig, bevor du etwas schreibst. Schreibe dann die Datei vast_optimizer.py mit dem Werkzeug datei_schreiben - gib dabei immer beide Angaben mit, pfad und inhalt. Inhalt: Konstanten (SCHWELLE 0.11, Ziel-Gesamtspeicher, Kostendeckel, Pfade), die eine Funktion vast(*args) fuer den Aufruf von /home/gh/venv_vastai/bin/vastai, das Lesen der laufenden Instanz, die Angebotssuche und die reine Bewertungsfunktion, die aus laufender Instanz und Angebotsliste das beste taugliche Angebot und die Ersparnis bestimmt. Achte auf den Abschnitt ueber den Grafikspeicher: gerechnet wird num_gpus mal gpu_ram, ein Ziel von 48 GB darf auch von zwei Karten zu je 24 GB erfuellt werden - nirgends num_gpus gleich 1. Dazu die Kommandozeile mit den Unterbefehlen pruefen, laufen und status und der Option --vram; laufen darf in dieser Stufe noch nichts umbuchen. Nur Standardbibliothek. Pruefe am Ende mit python3 -m py_compile vast_optimizer.py, dass die Datei uebersetzt."

stufe 2 test_vast_optimizer.py "Schreibe test_vast_optimizer.py mit unittest. Die Funktion vast wird per unittest.mock.patch ersetzt, die Tests laufen ohne Netz. Pruefe die Faelle aus AUFTRAG.md, die ohne Umbuchen auskommen: genau 11 Prozent kein Wechsel, 12 Prozent Wechsel, zu wenig Grafikspeicher, zwei Karten zu je 24 GB erfuellen ein Ziel von 48 GB, eine 24-GB-Karte erfuellt es nicht, ein Paar aus zwei 24-GB-Karten 20 Prozent billiger als die laufende 48-GB-Einzelkarte fuehrt zum Wechsel, Kostendeckel, keine laufende Instanz. Fuehre python3 -m unittest discover -v aus und behebe, was rot ist, bis alles gruen ist."

stufe 3 - "Baue das Umbuchen in vast_optimizer.py ein, genau in der Reihenfolge aus AUFTRAG.md Regel 5 und 6: neue Instanz mit demselben Abbild, Modell und Zugangstoken wie in referenz_gpu_mieten.py starten, warten bis ihr /health mit HTTP 200 antwortet (hoechstens 25 Minuten, in Schritten von 30 Sekunden), erst dann die alte Instanz zerstoeren. Kommt die neue nicht hoch, wird sie zerstoert und die alte bleibt. Bei mehreren Karten sieht der Container alle, -ngl 99 verteilt das Modell von selbst. Schreibe zustand.json (mit Zahl und Groesse der Karten) und eine Protokollzeile je Lauf nach optimizer.log. Uebersetzen pruefen."

stufe 4 - "Ergaenze die unterbrechbaren Angebote (interruptible, --type bid): mitsuchen, Gebot 15 Prozent ueber min_bid, und sie zaehlen nur als besser, wenn sie dieselben 11 Prozent schaffen. Ergaenze die Bremsen: Mindesthaltezeit 45 Minuten, hoechstens ein Wechsel je Stunde, Kostendeckel als Option --deckel mit Voreinstellung 0.60 Dollar je Stunde. Erweitere test_vast_optimizer.py um die restlichen Faelle aus AUFTRAG.md: unterbrechbares Angebot 20 Prozent billiger, Mindesthaltezeit laeuft noch, neue Instanz wird nicht gesund. Lass die Tests laufen, bis alles gruen ist."

stufe 5 - "Fuehre python3 vast_optimizer.py pruefen --vram 48 wirklich aus. Das kostet nichts, es fragt nur bei vast.ai an. Sieh dir mit vastai search offers und --raw eine echte Antwort an und pruefe Feld fuer Feld, ob die Namen und Einheiten stimmen, die du im Quelltext benutzt - besonders gpu_ram, num_gpus und dph_total. Vergewissere dich an echten Angeboten, dass Maschinen mit zwei oder vier Karten wirklich gefunden werden und nicht durch einen Filter herausfallen. Behebe jeden Fehler."

stufe 6 README.md "Schreibe README.md: Aufruf, Unterschied zwischen pruefen und laufen, die Option --vram mit dem Beispiel 48 GB aus zwei Karten, die cron-Zeile fuer einen Lauf alle 20 Minuten, und was jemand nachsehen muss, wenn ein Wechsel schiefgeht. Lass danach python3 -m unittest discover -v noch einmal laufen und melde das Ergebnis."

for i in 1 2 3 4 5 6; do
    if (cd "$P" && timeout 600 python3 -m unittest discover -v) >> "$LOG" 2>&1; then
        echo "===== Tests gruen im Anlauf $i, $(date "+%F %T") =====" >> "$LOG"
        break
    fi
    stufe "reparatur$i" - "Die Tests sind rot. Fuehre python3 -m unittest discover -v aus, lies die erste Fehlermeldung genau, sieh dir die betroffene Stelle im Quelltext an und behebe die Ursache - nicht den Test passend machen, ausser der Test selbst hat unrecht. Wiederhole, bis alles gruen ist."
done

echo "===== Bau beendet $(date "+%F %T") =====" >> "$LOG"
