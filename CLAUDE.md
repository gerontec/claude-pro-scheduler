# Master-KI Hinweise — gh@pve (192.168.5.23)

## Wichtigste Regel: Usage-Limit schonen

Das Claude-Abo hat ein Stunden-Limit. Wenn du merkst, dass eine Aufgabe
**rein textbasiert** ist (Analyse, Zusammenfassung, einfache Codegenerierung,
Netzwerk-Lookup, Erklärung) — delegiere sie an das lokale Batch-Job-System
statt sie selbst zu bearbeiten. Das schont dein Kontingent und ist 54× günstiger.

---

## Batch-Job-System (Sub-Agent-Delegation)

**API-Endpunkt:** `http://192.168.5.23/api/batch/api.php`  
**Auth-Header:** `X-API-Key: 2a61f527ded09cc2832cb49f8829f299`  
**Web-UI:** `http://192.168.5.23/api/batch/`

### Job einreichen (curl)

```bash
curl -s -X POST http://192.168.5.23/api/batch/api.php \
  -H "X-API-Key: 2a61f527ded09cc2832cb49f8829f299" \
  -H "Content-Type: application/json" \
  -d '{
    "model":   "xiaomi",
    "prompt":  "Deine Aufgabe hier …",
    "targetdate": "'"$(date +%Y-%m-%d)"'"
  }'
# → {"id": 42, "status": "queued", "model": "xiaomi"}
```

### Job-Status prüfen

```bash
curl -s "http://192.168.5.23/api/batch/api.php?id=42&full=1&apikey=2a61f527ded09cc2832cb49f8829f299"
# → {"status":"done","result":"…","cost_usd":"0.000538"}
```

### Auf Ergebnis warten

```bash
while true; do
  STATUS=$(curl -s "http://192.168.5.23/api/batch/api.php?id=42&apikey=2a61f527ded09cc2832cb49f8829f299" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$STATUS" = "done" ] || [ "$STATUS" = "failed" ] && break
  sleep 5
done
```

### Bis zu 9 Jobs parallel einreichen

```bash
for prompt in "Aufgabe 1" "Aufgabe 2" "Aufgabe 3"; do
  curl -s -X POST http://192.168.5.23/api/batch/api.php \
    -H "X-API-Key: 2a61f527ded09cc2832cb49f8829f299" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"xiaomi\",\"prompt\":\"$prompt\"}" &
done
wait
```

---

## Entscheidungsmatrix: Welches Modell wann?

### Stufe 1 — Kostenlos (immer bevorzugen)

| Aufgabe | Empfohlenes Modell | Model-ID |
|---|---|---|
| Standardaufgabe, Analyse, Erklärung | **xiaomi** | *(alias für Xiaomi MiMo V2 Flash)* |
| Code-Generierung / Debugging | **qwen-free** | *(alias für Qwen3 Coder 480B free)* |
| Komplexes Reasoning, lange Dokumente | `nvidia/nemotron-3-super-120b-a12b:free` | 120B, bestes freies Modell |
| Allgemein-Reasoning mittelkomplex | `nousresearch/hermes-3-llama-3.1-405b:free` | 405B, sehr ausgewogen |
| Research / Faktensuche | `openai/gpt-oss-120b:free` | OpenAI-basiert, 120B |
| Mehrstufiges Denken (Chain-of-Thought) | `qwen/qwen3-next-80b-a3b-instruct:free` | hat internen Thinking-Modus |
| Code + Reasoning kombiniert | `meta-llama/llama-3.3-70b-instruct:free` | Meta Llama 3.3 70B |
| Bild-/Multimodal-Aufgabe | `nvidia/nemotron-nano-12b-v2-vl:free` | Vision+Language, 12B |
| Schnelle kurze Aufgabe (Latenz wichtig) | `openai/gpt-oss-20b:free` | 20B, schneller als 120B |
| Deutsch-Aufgabe, großes Modell nötig | `inclusionai/ling-2.6-1t:free` | 1T-Parameter MoE |
| OCR / Dokumententext extrahieren | `baidu/qianfan-ocr-fast:free` | spezialisiert auf OCR |
| Unbekannte Aufgabe / Auto-Routing | `openrouter/free` | OpenRouter wählt selbst |

### Stufe 2 — Kostenpflichtig (nur wenn Free-Qualität nicht reicht)

| Aufgabe | Modell | Kosten |
|---|---|---|
| Komplexes Reasoning, Produktion | **mimo-pro** | ~/bin/bash.01/Job |
| Großer Code-Kontext (>32k Token) | **qwen** | ~/bin/bash.005/Job |

### Stufe 3 — Claude Abo (nur für Tool-Zugriff oder Claude-Qualität nötig)

| Aufgabe | Modell | Abo-Gewicht |
|---|---|---|
| Einfache Claude-Antwort, kein Tool | **haiku** | 1× |
| Mittlere Komplexität + Tools | **sonnet** | 4× |
| Sehr komplex, kritisch, Datei-Ops | **opus** | 19× |

### Kurzregeln

1. **Kein Bash/Datei-Zugriff nötig → immer delegieren, nie selbst**
2. **Delegieren: erst Free → dann mimo-pro → dann Claude**
3. **Code → qwen-free; Reasoning → nemotron-3-super-120b; Allgemein → xiaomi**
4. **Multimodal (Bild) → nemotron-nano-12b-vl** *(einziges freies Vision-Modell)*
5. **OCR → qianfan-ocr-fast**
6. **Mehrere unabhängige Teilaufgaben → parallel als separate Jobs einreichen**

---

## Sub-Agent-Kontext (automatisch injiziert)

Jeder Job erhält automatisch als Prompt-Prefix:
- **`ki_localhost_cache`** — aktueller Systemzustand (RAM, Disk, OpenRouter-Guthaben)
- **`ki_infrastructure`** — alle Netzwerkgeräte mit IP, Ports, Zweck

Sub-Agents können daher direkt auf Geräte referenzieren (z. B. Kodi TV = `192.168.178.43`,
MQTT-Broker = `192.168.178.218:1883`) ohne dass du die IPs im Prompt angeben musst.

---

## Wann NICHT delegieren

- Aufgaben die Bash/Datei-Zugriff benötigen → selbst erledigen
- Interaktive Loops (der Sub-Agent antwortet einmalig, kein Dialog)
- Aufgaben < 5 Sekunden Denkzeit — Overhead lohnt sich nicht
- Aufgaben die Claude CLI benötigen (z. B. Code committen, SSH-Befehle)

---

## OpenRouter-Guthaben

Aktueller Stand in MariaDB:
```bash
mysql -u gh -pa12345 wagodb \
  -e "SELECT label, value FROM ki_localhost_cache WHERE category='openrouter' ORDER BY label"
```

---

## delegate — Direkt-Befehl ohne API-Key (empfohlen)

Statt curl: einfach `delegate` aufrufen — Key ist eingebaut.

```bash
delegate "Aufgabe …"                   # Job einreichen
delegate --wait "Aufgabe …"            # einreichen + auf Ergebnis warten
delegate --model mimo-pro "Aufgabe …"  # anderes Modell
delegate --model nvidia/nemotron-3-super-120b-a12b:free "Aufgabe …"
delegate --list                         # letzte Jobs anzeigen
delegate --status 42                    # Job-Status + Ergebnis
```

Verfügbar auf jedem PC nach: `curl -O https://raw.githubusercontent.com/gerontec/claude-pro-scheduler/main/delegate.py && chmod +x delegate.py`
