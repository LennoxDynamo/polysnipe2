# PolySnipe v2

BTC 5-min Polymarket Trading Simulator — Python backend + Web frontend.

## Lokal starten

```bash
cd polysnipe2/backend
pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000
```

### Windows/PowerShell Hinweise

Wenn `uvicorn` als Befehl nicht gefunden wird, nutze Modul-Start mit Python:

Aus `backend/`:

```powershell
python -m uvicorn main:app --reload --port 8000
```

Aus Repo-Root (`polysnipe2/`):

```powershell
python -m uvicorn --app-dir backend main:app --reload --port 8000
```

Dann `frontend/index.html` im Browser öffnen — oder: http://localhost:8000/app

## Auf Railway deployen

1. Repo auf GitHub pushen
2. Auf railway.app: "New Project → Deploy from GitHub Repo"
3. Railway erkennt `Procfile` und `requirements.txt` automatisch
4. Nach dem Deploy läuft Frontend + Backend unter derselben URL

## Eigene Strategie schreiben

Neue `.py` Datei in `backend/strategies/` ablegen:

```python
from __base__ import BaseStrategy, Signal

class Strategy(BaseStrategy):
    NAME = "Meine Strategie"
    PARAMS = {
        "entry_max_price": {"type":"float","default":0.28,"min":0.05,"max":0.45,"step":0.01,"label":"Kaufschwelle"},
    }
    def on_tick(self, ctx) -> Signal:
        if ctx.down_price <= self.params["entry_max_price"]:
            return Signal("BUY", "DOWN", ctx.down_price, reason="Signal!", tp=0.38, sl=0.12)
        return Signal("HOLD")
```

Backend neu starten → Strategie erscheint automatisch in der UI.

## API Endpunkte

| Methode | Pfad | Beschreibung |
|---------|------|--------------|
| GET | `/api/health` | Status + BTC-Preis |
| GET | `/api/strategies` | Alle Strategien mit Parametern |
| GET | `/api/markets/btc5min` | Aktive BTC 5-min Märkte |
| GET | `/api/markets/search?q=...` | Marktsuche |
| GET | `/api/market/{id}` | Markt + Orderbook |
| GET | `/api/context/{id}` | Live MarketContext |
| GET | `/api/signal/{id}?strategy=...` | Signal für Markt |
| POST | `/api/simulate` | Backtest |
| POST | `/api/compare` | Alle Strategien vergleichen |
| POST | `/api/export/csv` | Trades als CSV |
| POST | `/api/export/json` | Ergebnis als JSON |
