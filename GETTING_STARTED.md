# Getting Started — Legacy Brands Money Making Machine

A SEPA-style stock scanner that pulls live data from Yahoo Finance, applies
Mark Minervini's filtering rules, and produces a daily candidate list with
deep-dive analysis for each name.

This guide is for a **friend or collaborator** who's been invited to the
private GitHub repo and wants to run the scanner on their own computer.

It assumes Windows. If you're on Mac, the steps are nearly identical — substitute the obvious things (e.g. `python3` instead of `python`, forward slashes in paths, Terminal instead of cmd).

---

## What you'll need (~30 minutes the first time)

1. A free GitHub account (if you don't have one: https://github.com → Sign up)
2. Python 3.10 or newer
3. Git for Windows
4. Acceptance of the collaborator invite (the repo owner will send this)

You'll only do steps 2-4 once. After that, running the scanner is just
double-clicking a `.bat` file.

---

## Step 1 — Accept the collaborator invite

When the repo owner adds you, GitHub sends an email to your account address.
Click the link in the email (or check your notifications at https://github.com
in the bell icon at top-right). Click **Accept invitation**.

After accepting, you'll be able to view the repo at
`https://github.com/<owner>/LB-stock-scanner` and clone it to your machine.

---

## Step 2 — Install Python

1. Go to **https://www.python.org/downloads/**
2. Click the big yellow "Download Python 3.X.X" button (current latest)
3. Run the installer
4. **IMPORTANT:** on the very first installer screen, check the box at
   the bottom that says **"Add python.exe to PATH"** before clicking
   "Install Now". This is critical — without it, your computer won't
   find Python from the command line.
5. Click "Install Now" and wait
6. When done, open a new Command Prompt (Windows key → type `cmd` → Enter)
   and verify with:
   ```
   python --version
   ```
   You should see something like `Python 3.12.X`. If you see "not recognized",
   Python wasn't added to PATH — uninstall and reinstall with the checkbox.

---

## Step 3 — Install Git

1. Go to **https://git-scm.com/download/win**
2. The download starts automatically; run the installer when done
3. The installer has ~11 screens of options. Just click **Next** through
   all of them — defaults are fine. One small recommendation: on the
   "Choosing the default editor" screen, pick **"Use Notepad as Git's
   default editor"** from the dropdown (the default Vim editor is harder
   to use for non-coders).
4. After install, open a **new** Command Prompt and verify with:
   ```
   git --version
   ```
   You should see `git version 2.X.X.windows.1` or similar.
5. Configure Git with your identity:
   ```
   git config --global user.name "your-github-username"
   git config --global user.email "your-email@example.com"
   ```
   Use the same email as your GitHub account.

---

## Step 4 — Clone the repo to your computer

1. Pick a location for the code on your computer. Common choice:
   `C:\Users\<your-name>\Documents\` or your Desktop.
2. Open Command Prompt and navigate there:
   ```
   cd "C:\Users\<your-name>\Documents"
   ```
   (Use your actual user name and folder.)
3. Clone the repo:
   ```
   git clone https://github.com/<owner>/LB-stock-scanner.git
   ```
   (The repo owner will give you the exact URL.) On first run, a browser
   window may pop up asking you to authenticate to GitHub — sign in and
   authorize, same as the repo owner did.
4. After cloning, a new folder called `LB-stock-scanner` will appear
   with all the code. Enter it:
   ```
   cd LB-stock-scanner
   ```

---

## Step 5 — Set up the Python environment

The scanner uses a self-contained Python environment so its packages
don't conflict with anything else on your computer. We create that
environment now (one-time setup).

1. From inside the `LB-stock-scanner` folder:
   ```
   python -m venv .venv
   ```
   This creates a `.venv` folder. Takes 5-10 seconds. No output expected.

2. Activate the environment:
   ```
   .venv\Scripts\activate
   ```
   You'll see `(.venv)` prepended to your command prompt — that means
   the environment is active.

3. Install the required Python packages:
   ```
   pip install -r requirements.txt
   ```
   This downloads and installs ~30 packages. Takes 2-5 minutes depending
   on your internet. Output will scroll a lot — that's normal.

---

## Step 6 — Create your own secrets file

The repo deliberately doesn't include the `secrets.toml` file (it
contains private values like passwords). You create your own local copy.

1. Inside the `LB-stock-scanner` folder, navigate to `.streamlit\`:
   ```
   cd .streamlit
   ```
2. Copy the example file:
   ```
   copy secrets.toml.example secrets.toml
   ```
3. Open the new `secrets.toml` in Notepad and leave `app_password = ""`
   (empty) — that disables the password gate so you can use the scanner
   directly on your own machine without typing a password every time.
4. Save and close.
5. Go back to the project root:
   ```
   cd ..
   ```

---

## Step 7 — Run the scanner

1. From the `LB-stock-scanner` folder, double-click **`run.bat`**.
2. A Command Prompt window will open and start the Streamlit server.
3. Wait ~5-10 seconds — your default browser should auto-open to
   `http://localhost:8501` with the scanner dashboard.
4. If the browser doesn't open automatically, manually open it and
   type `localhost:8501` in the address bar.

The dashboard is now running on your machine.

---

## Step 8 — Run your first scan

1. In the sidebar on the left, make sure **ELITE MODE** is toggled on
   (red toggle, with text "ELITE MODE (RS≥85, EPS REQUIRED)").
2. Click **▶ Start Scan**.
3. Wait. **The first scan takes 20-40 minutes** because it has to
   download price history for ~3,000 stocks from Yahoo Finance.
   Subsequent scans are much faster (5-10 minutes) because data is
   cached locally.
4. While waiting, you can leave the cmd window open in the background
   — Streamlit runs the scan continuously and updates the dashboard
   when done.
5. When complete, you'll see a list of candidates with their RS rating,
   signals, warnings, and suggested buy/stop prices. Click any ticker
   to drill into the deep-dive page.

---

## Pulling updates later

When the repo owner makes improvements, you can fetch their changes
without re-doing all the setup. From the `LB-stock-scanner` folder:

```
git pull
```

That downloads any new code. If new Python packages were added, also run:

```
.venv\Scripts\activate
pip install -r requirements.txt
```

---

## Troubleshooting

**Scan returns 0 candidates** — Yahoo Finance may be rate-limiting your IP
because you ran multiple scans in quick succession. Wait an hour, delete
`data\cache\fundamentals.json` to force a fresh re-fetch, and re-run.

**"python is not recognized"** — Python wasn't added to PATH during install.
Uninstall Python and reinstall, this time checking the
"Add python.exe to PATH" box on the first installer screen.

**"git is not recognized"** — Same issue with Git. Reinstall Git and pay
attention to the PATH option screen.

**Scan crashes mid-way with an error** — Screenshot the error from the
cmd window and send it to the repo owner. Almost always either a yfinance
schema change or a network issue.

**Dashboard says "No candidates passed the screen"** — Either the market
is genuinely in a bad regime, or the fundamentals data is stale. Try
deleting `data\cache\fundamentals.json` and re-scanning.

---

## What's in this scanner

- **Long scanner** — Minervini SEPA-style filter for stocks setting up
  for breakouts (RS≥85 in elite mode, EPS+ and Sale+ fundamentals,
  trend template, VCP detection, pivot points, stop placement).
- **Short scanner** — bearish setup detection (RSI2, distribution days,
  climax runs, BB rejections).
- **Rotation page** — industry-level RS rankings and momentum deltas.
- **Social tab** — ticker mentions across WSB, r/stocks, r/investing,
  etc., pulled live from Reddit's public JSON endpoint.
- **Deep-dive page** — per-ticker view with price chart, Fibonacci
  retracement overlays, VCP zones, key ratios, ownership data,
  Reddit mentions, and a Trade Now execution panel.

---

## Questions?

Ping the repo owner. They've been through every weird edge case in
this codebase (sometimes multiple times) and can help debug.
