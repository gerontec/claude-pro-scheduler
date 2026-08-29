# Auftrag: vast_optimizer.py

Gebaut wird ein Programm, das die gemietete GPU bei vast.ai laufend guenstiger
macht: es sucht regelmaessig die besten Angebote, vergleicht sie mit der
laufenden Instanz und bucht bei einem Preisvorteil von mehr als 11 % auf das
bessere Angebot um - mit automatischer Einrichtung, ohne Handgriff.

Vorlage ist `referenz_gpu_mieten.py` (liegt in diesem Verzeichnis). Dort steht
schon, wie gesucht, gestartet, geprueft und gestoppt wird. Diese Aufrufe
werden uebernommen, nicht neu erfunden.

## Zieldateien in diesem Verzeichnis

* `vast_optimizer.py`       das Programm
* `test_vast_optimizer.py`  Unittests, laufen ohne Netz und ohne Geld
* `README.md`               kurze Bedienung, Einbau in cron

## Aufrufe

    python3 vast_optimizer.py pruefen              # nur rechnen und sagen, was waere
    python3 vast_optimizer.py laufen               # wirklich umbuchen, wenn es lohnt
    python3 vast_optimizer.py status               # laufende Instanz, Preis, Endpunkt
    python3 vast_optimizer.py pruefen --vram 48    # Ziel: 48 GB Grafikspeicher

`pruefen` ist die Voreinstellung: ohne `laufen` wird nie Geld ausgegeben.

## Grafikspeicher wird in der Summe gerechnet - das ist die wichtigste Regel

Gesucht wird nicht eine Karte, sondern eine Speichermenge. Ein Ziel von 48 GB
ist genauso mit **zwei Karten zu je 24 GB** erfuellt wie mit einer 48-GB-Karte,
und oft ist das Paar billiger. Also:

* `--vram` gibt den **Gesamtspeicher** vor, Voreinstellung 24, typisch 48.
* Ein Angebot ist tauglich, wenn `num_gpus * gpu_ram >= Ziel`. Nirgends steht
  `num_gpus == 1`, weder im Suchfilter noch in der Bewertung.
* Auch die laufende Instanz wird so gemessen: `num_gpus * gpu_ram`.
* Die Karten eines Angebots sind gleich (ein `gpu_name`, eine `gpu_ram`).
* Welches Feld vast.ai fuer den Speicher je Karte fuehrt und in welcher
  Einheit (MB oder GB), pruefst du an einer echten Antwort
  (`vastai search offers ... --raw`) - nicht aus der Erinnerung. Dasselbe gilt
  fuer den Gesamtspeicher, falls es ein eigenes Feld dafuer gibt.
* Beim Start bekommt der Container alle Karten der Instanz. llama-server
  verteilt das Modell mit `-ngl 99` von selbst ueber alle sichtbaren Karten;
  `--tensor-split` braucht es nur bei ungleichen Karten. Mehr Speicher heisst
  ausserdem: der Kontext darf so gross bleiben wie vorgegeben.

## Regeln

1. Vergleichsgroesse ist `dph_total` (Dollar je Stunde, alles inbegriffen)
   der laufenden Instanz gegen `dph_total` des Angebots.
2. Umgebucht wird erst ab **mehr als 11 %** Ersparnis (`SCHWELLE = 0.11`).
   Rechnung: (alt - neu) / alt > SCHWELLE.
3. Ein Angebot kommt nur in Frage, wenn es mindestens gleichwertig ist:
   `num_gpus * gpu_ram` >= Ziel (siehe oben), `disk_space` >= 60 GB,
   `inet_down` >= 200 Mbit/s, `reliability2` >= 0.97, `rentable` = true.
4. Unterbrechbare Angebote (interruptible, Gebotsverfahren) sind erlaubt und
   werden mitgesucht (`--type bid`). Das Gebot liegt 15 % ueber `min_bid`.
   Eine unterbrechbare Instanz zaehlt nur als besser, wenn sie die 11 %
   ebenfalls schafft - das Risiko wird nicht mit ein paar Cent bezahlt.
5. Umgebucht wird in dieser Reihenfolge, nie andersherum:
   neue Instanz starten -> warten, bis ihr `/health` mit HTTP 200 antwortet
   (hoechstens 25 Minuten) -> erst dann die alte Instanz zerstoeren.
   Kommt die neue nicht hoch, wird sie zerstoert und die alte bleibt stehen.
6. Die neue Instanz wird genauso eingerichtet wie in `referenz_gpu_mieten.py`:
   dasselbe Abbild, dasselbe Modell ueber `-hf`, derselbe Zugangstoken aus
   `~/.config/llm_fern/api_key`. Das ist die "automatische Einrichtung".
7. Bremsen gegen Hin- und Herspringen:
   * Mindesthaltezeit 45 Minuten nach dem letzten Wechsel.
   * Hoechstens ein Wechsel je Stunde.
   * Kostendeckel als Option `--deckel`, Voreinstellung 0.60 Dollar je Stunde:
     teurer wird nichts gestartet. Zwei Karten kosten naturgemaess mehr, der
     Deckel gehoert darum zum Ziel und ist keine feste Zahl im Quelltext.
8. Zustand steht in `zustand.json` (Instanznummer, Preis, Zahl und Groesse der
   Karten, Zeitpunkt des letzten Wechsels, Endpunkt). Jeder Lauf schreibt eine
   Zeile nach `optimizer.log`.
9. Laeuft gar keine Instanz, wird nichts gestartet - der Optimizer verwaltet,
   er kauft nicht von sich aus ein. Er sagt nur, was das beste Angebot waere.

## Anforderungen an den Quelltext

* Nur die Standardbibliothek, kein pip.
* Die `vastai`-Aufrufe gehen ueber **eine** Funktion, damit die Tests sie
  ersetzen koennen. Pfad wie in der Vorlage: /home/gh/venv_vastai/bin/vastai
* Deutsche Kommentare, die begruenden, warum etwas so ist, nicht was die
  Zeile tut.
* Jede Entscheidung des Programms muss aus dem Protokoll nachvollziehbar sein.

## Anforderungen an die Tests

* `python3 -m unittest discover` laeuft gruen, ohne Netz, ohne vastai.
* Gemockt wird die eine vastai-Funktion, mit echt aussehenden JSON-Antworten.
* Geprueft werden mindestens:
  - 11 % genau getroffen -> kein Wechsel (Schwelle ist "mehr als")
  - 12 % billiger -> Wechsel
  - billiger, aber zu wenig Grafikspeicher -> kein Wechsel
  - **zwei Karten zu je 24 GB erfuellen ein Ziel von 48 GB -> tauglich**
  - **eine Karte mit 24 GB erfuellt ein Ziel von 48 GB nicht -> untauglich**
  - **Paar aus zwei 24-GB-Karten ist 20 % billiger als die laufende
    48-GB-Einzelkarte -> Wechsel**
  - billiger, aber Mindesthaltezeit laeuft noch -> kein Wechsel
  - unterbrechbares Angebot 20 % billiger -> Wechsel, Gebot = min_bid * 1.15
  - neue Instanz wird nicht gesund -> neue zerstoert, alte bleibt
  - keine Instanz laeuft -> nichts wird gestartet
  - Kostendeckel greift
