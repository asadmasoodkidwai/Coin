# Coin Board

An AI "board of directors" for paper trading. Four specialist analysts report to a reasoning-model Chair, which decides **buy, sell or ignore**. A rule-based Risk Firewall can block anything the AI proposes. Profits are split automatically into a safety reserve, reinvested capital and withdrawable cash, and money moves in and out through **PayPal** (sandbox).

Built for the *Build What's Next with PayPal and AI* hackathon. **Paper money and PayPal sandbox only. Not financial advice.**

## What it does

- **Ask the Board**: pick a stock from a searchable dropdown (company names and logos). Tabs show the **live market chart**, the **evidence** (every news article the sentiment analyst read, with a tone label and link), the **prediction** (Kronos's simulated futures and range) and a **detailed explanation** with the safety checks.
- **The board**: Market (technical indicators), Forecast ([Kronos](https://github.com/shiyu-coder/Kronos)), Sentiment ([FinBERT](https://huggingface.co/ProsusAI/finbert)) and Company (fundamentals) analysts, then the Chair. Serious news (investigations, lawsuits, fraud) is flagged and blocks buying.
- **Risk Firewall**: server-side limits on position size, loss per trade, daily loss, confidence, cash and news risk. Sells and stop-loss exits are never blocked.
- **Vault**: when you close a day, profit is split into a reserve (refills later losses), withdrawable cash and reinvested capital. A large drawdown pauses trading until you resume.
- **Automation**: a watchlist scanner screens up to 200 stocks, reviews the best candidates in depth, and in autopilot mode trades by itself. Stop-loss and target exits run every minute. The daily close can run automatically.
- **PayPal**: funds are added with the official PayPal Checkout button (Orders API, captured on the server) and profit is withdrawn with PayPal Payouts.
- **Resilient reasoning**: if the main model hits a rate limit, an optional backup model takes over, then a small **student model** that learns from the main model, from real outcomes and from your approvals. It earns a role over time: apprentice, then a second voice that can shrink or cancel a buy, then decider, based on evidence and not just time, with automatic demotion.
- **Live**: everything on every tab updates by itself over a server-sent event stream, including automated trades and scans.

## AI and PayPal tools used

| Tool | How it is used |
|---|---|
| [Kronos-small](https://github.com/shiyu-coder/Kronos) (K-line foundation model) | Simulates several 5-day price futures; their median is the forecast and the middle 80% is the range |
| [FinBERT](https://huggingface.co/ProsusAI/finbert) | Scores the tone of each news headline |
| Chair: any OpenAI-compatible reasoning model (default Groq `openai/gpt-oss-120b`) | Reads all analyst reports and the headlines, decides, and writes the explanation |
| Optional backup model (for example a local model through [Ollama](https://ollama.com)) | Takes over when the main model is rate limited |
| Student model (numpy, built here) | Imitates the Chair, estimates how often similar buys worked, and exports training data |
| PayPal Orders API v2 + JavaScript SDK buttons | Funding the vault (order created and captured on the server) |
| PayPal Payouts API | Withdrawing profit |
| yfinance | Free market data and news (delayed, unofficial) |

## Quick start

Needs **Python 3.10 to 3.12** (3.12 recommended). Git is only needed for Kronos.

**Minimal, works with no keys (demo mode):**
```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell   (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python -m uvicorn app.main:app
```
Open http://localhost:8000. In demo mode funds are labelled demo funds, the Chair uses a fixed rule, and Forecast and Sentiment use labelled baselines.

**Full setup (real models):**
```bash
pip install -r requirements-models.txt
git clone https://github.com/shiyu-coder/Kronos ../Kronos
pip install -r ../Kronos/requirements.txt
cp .env.example .env                # then fill in the values below
```
On Windows you can also double-click `setup.bat` once (it installs everything and asks for a Groq key), then `run.bat` next time.

## Configuration (`.env`, git-ignored)

| Variable | Purpose |
|---|---|
| `CHAIR_BASE_URL`, `CHAIR_MODEL`, `CHAIR_API_KEY` | Main reasoning model (any OpenAI-compatible API). Example: `https://api.groq.com/openai/v1`, `openai/gpt-oss-120b` |
| `CHAIR2_BASE_URL`, `CHAIR2_MODEL`, `CHAIR2_API_KEY` | Optional backup model, for example Ollama: `http://localhost:11434/v1`, `qwen3:8b`, `ollama` |
| `KRONOS_REPO` | Path to the cloned Kronos repo. Optional `KRONOS_SAMPLES` (futures to simulate, default 10) and `KRONOS_DEVICE` |
| `PAYPAL_CLIENT_ID`, `PAYPAL_SECRET`, `PAYPAL_PAYOUT_EMAIL` | PayPal **sandbox** app credentials and the sandbox account that receives payouts |
| `APP_BASE_URL` | Only for the redirect fallback (default `http://localhost:8000`) |
| `COIN_TOKEN` | Optional: require this token on state-changing requests |
| `COIN_DB` | Database path (default `coin.db`) |

## PayPal sandbox setup (about 10 minutes, free)

1. Log in at developer.paypal.com. Under **Apps & Credentials** (Sandbox), create an app and copy the **Client ID** and **Secret**. Make sure **Payouts** is enabled for it.
2. Under **Testing tools, Sandbox accounts**, open the default **Personal** account and note its email and password (sandbox only).
3. Put the three `PAYPAL_*` values in `.env` and restart. The PayPal card on Home shows **Sandbox connected**.
4. **Add funds**, click the PayPal button, log in with the sandbox personal account and approve. To see the payment, sign in at sandbox.paypal.com with the sandbox business account.

The server sets each amount itself, only credits orders it created, credits each order once, and checks that PayPal's captured amount matches. A payout is sent before money leaves the vault, so a failed payout loses nothing. The secret never reaches the browser.

## How the pieces fit

```
Yahoo data -> Market / Forecast (Kronos) / Sentiment (FinBERT) / Company analysts
           -> Chair (main model -> backup -> student -> fixed rule)  [second voice: student]
           -> Risk Firewall (server-side)  -> paper order -> Vault (reserve, reinvest, withdraw)
                                                           -> PayPal deposits and payouts
```

`app/` holds the code: `analysts.py` (data and analysts), `chair.py` (model chain), `learn.py` (student model), `risk.py` (firewall), `vault.py` (ledger, SQLite), `paypal.py`, `main.py` (API, scanner, live stream). `static/index.html` is the whole front end.

## Tests

```bash
python -m pytest -q tests
```
The tests cover the firewall, ledger, scanner, model fallback, student promotion and the PayPal flow. The PayPal tests run against `tests/fake_paypal.py`, a local stand-in for PayPal's API that is used only by the tests. The live sandbox flow is exercised by running the app with your own sandbox keys.

## Limitations

- Paper trading only, long-only, US stocks. Free market data is delayed and can be wrong.
- The reserve is a buffer, not a guarantee. A strategy that loses more than it wins will drain it.
- The student model cannot read headlines or reason about new situations. The news gate and the safety checks apply whoever decides.
- Do not use this with real money without extensive testing.

## License

MIT. See `LICENSE`.

## Updating an existing Windows installation while keeping your dashboard

This archive retains the tabbed **Home / Portfolio / History / Ask the Board** dashboard shown in the earlier `g` build and adds the later `i` and `j` features. Extract the archive to a temporary folder and run `INSTALL-WINDOWS.ps1` from the extracted `coin-board` folder. The updater backs up the old application source, replaces `app/`, `static/`, and `tests/`, installs requirements, executes tests, and starts the server at http://localhost:8000. It leaves your existing `C:\coin-board\.env`, `C:\coin-board\coin.db`, and `.venv` in place. **Do not delete your existing database if you want to retain your balances and history.**

PowerShell example (after extracting):

```powershell
cd "$HOME\Downloads\coin-board"
.\INSTALL-WINDOWS.ps1
```

If PowerShell blocks local scripts, run `powershell -ExecutionPolicy Bypass -File .\INSTALL-WINDOWS.ps1` instead. If port 8000 is already occupied, stop the prior Coin Board server before running the updater.
