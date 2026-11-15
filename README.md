# SportBayes – NBA, NHL a fotbal

Živé výsledky, kurzy a Bayesovský pravděpodobnostní model pro NBA (basketbal), NHL (hokej) a pět fotbalových lig
(Premier League, La Liga, Serie A, Ligue 1, Bundesliga). U fotbalu model počítá tři výsledky: domácí / remíza / hosté.
Mezi ligami se přepíná tlačítky nahoře v dashboardu. V tabulce je vždy napřed domácí tým, potom hosté.
Potřebuje jen Python 3.9+ a numpy.

## Spuštění

- Dvojklik na `spust.bat`, nebo v konzoli `python sportbayes.py`.
- Otevře se dashboard na http://127.0.0.1:8765/ a sám se obnovuje každou minutu.
- První spuštění stáhne 4 sezóny výsledků (asi minutu), další už jen doplňují nové zápasy.

Další příkazy:

| Příkaz | Co dělá |
|---|---|
| `python sportbayes.py test` | ověří, že funguje zdroj dat – spusť jako první |
| `python sportbayes.py backtest` | vyladí parametry na starších sezónách, otestuje na novějších, uloží do `config.json` |
| `python sportbayes.py today` | dnešní zápasy do konzole |

Za příkaz lze přidat ligu: `nba`, `nhl`, `epl`, `laliga`, `seriea`, `ligue1`, `bundesliga`
(např. `python sportbayes.py backtest nhl`).
Ligy, které se načítají, určuje `leagues` v `config.json`.

## Sázkový deník

- Záložka **Deník sázek** (`denik.html`). U každého zápasu před začátkem jsou tlačítka **D / R / H**
  (domácí / remíza / hosté): klikneš, zadáš kurz a vklad a sázka se zapíše do deníku
  (kurz a doporučený vklad se předvyplní). Sázku z jiného zdroje přidáš formulářem.
- Zapsané sázky se po odehrání zápasu samy vyhodnotí (výhra / prohra); u extraligy podle výsledku po 60 minutách,
  stejně jako to počítá model. Vyhodnocuje-li tvoje sázkovka jinak, přepiš stav v rozbalovátku.
  Kurz, vklad, stav i poznámku lze kdykoliv opravit, sázku smazat tlačítkem ✕.
- Nahoře je přehled: počet sázek, vsazeno, zisk, návratnost, úspěšnost. Deník jde stáhnout jako CSV pro Excel.
- Uloženo v `denik.json` (kopie předchozího stavu v `denik_zaloha.json`). Soubor je v `.gitignore`,
  takže se nikdy nedostane na GitHub. Na stránkách z GitHubu se deník ukládá jen v prohlížeči.

## Nastavení lig

Záložka **⚙ Nastavení** (`nastaveni.html`): u každé ligy lze zrušit zobrazení a měnit pořadí šipkami.
Skrytou ligu aplikace nenačítá ani pro ni nestahuje data (šetří denní limit dotazů u českých lig);
po zapnutí se načte na pozadí. Uloženo v `nastaveni.json`. Ligy, které aplikace vůbec smí načíst,
určuje `leagues` v `config.json`.

## Model

Dynamická gaussovská Bayesovská síť (`model.py`):

    síla domácích ─┐
    síla hostů ────┤
    domácí výhoda ─┼─> bodový rozdíl ─> výhra domácích
    únava (B2B) ───┘

- Síla každého týmu je skrytá veličina s vlastní nejistotou; po každém zápase se
  aktualizuje Bayesovým pravidlem a mezi zápasy se pomalu mění.
- Model se učí z bodového rozdílu, ne jen z výhry/prohry – výhra o 25 říká víc než o 1.
- Protože je síť lineární a gaussovská, inference je přesná (Kalmanův filtr), bez MCMC.
- Model nezná zranění, sestavy ani odpočinek hvězd. To je jeho hlavní slabina.

## Kurzy

- Základ: kurzy z veřejného zdroje výsledků (americké sázkovky, přepočtené na desetinné).
- Vlastní kurz (Tipsport, Fortuna...) napiš v dashboardu do sloupce „Můj kurz“.
- Volitelně: bezplatný klíč z the-odds-api.com vlož do `config.json` jako `odds_api_key`
  – použijí se nejlepší kurzy evropských sázkovek (dotaz jednou za 2 hodiny kvůli limitu).

## Nastavení (`config.json`)

- `min_edge` – od jaké výhodnosti zobrazit tip (0.05 = 5 %)
- `bankroll`, `kelly_fraction`, `max_stake_pct` – doporučený vklad (čtvrtinový Kelly, strop 3 % banku)
- `refresh_sec`, `port`, `seasons_back`

## Soubory

- `denik.json` – tvůj sázkový deník, `nastaveni.json` – zobrazení lig
- `data/games.csv`, `data/nhl_games.csv` – historie výsledků
- `data/predictions.csv`, `data/nhl_predictions.csv` – předzápasové předpovědi a kurzy; z nich se počítá „Bilance modelu“

## České ligy (Extraliga, 1. fotbalová liga)

- Data jsou z API-Sports. Bezplatný klíč vlož do `config.json` jako `apisports_key`
  (soubor s klíčem nikam nezveřejňuj).
- Bezplatný tarif: 100 dotazů denně, celé sezóny jen 2022–2024, aktuální zápasy jen včera/dnes/zítra.
  Model se proto naučí ze tří starších sezón a letošní výsledky sbírá den po dni, dokud aplikace běží.
  Dny, kdy aplikace neběžela, v historii chybí.
- Extraliga se počítá po základní hrací době (domácí / remíza / hosté).
- Kurzy pro české ligy zdarma nejsou – zadávají se ručně do sloupce „Můj kurz“.
- Ligy: `extraliga`, `czliga`.
