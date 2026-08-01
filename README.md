# Sit housing monitor (Trondheim)

Checks [bolig.sit.no](https://bolig.sit.no/en/) every 10 minutes for **newly
available student housing in Trondheim** and sends a **push notification to your
phone** via [ntfy.sh](https://ntfy.sh) the moment a new unit appears.

It runs entirely on **GitHub Actions** (free), so your PC does not need to be on.

---

## How it works

`bolig.sit.no` loads its live availability from a GraphQL API. The script
(`check_sit_housing.py`) calls that API directly, filters to Trondheim, compares
the result against the last-seen list stored in `state/seen.json`, and pushes a
notification for anything new. No browser, no scraping, no dependencies (pure
Python standard library).

---

## One-time setup (~10 minutes)

### 1. Install ntfy on your phone and pick a topic

1. Install the **ntfy** app: [App Store](https://apps.apple.com/app/ntfy/id1625396347) · [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy).
2. In the app, tap **+** to subscribe to a **topic**. A topic is just a secret
   name — anyone who knows it can read/write it, so make it long and unguessable,
   e.g. `sit-trondheim-7hK2pQ9x`.
3. Remember that exact topic string — you'll need it in step 3.

> Tip: to test, open `https://ntfy.sh/YOUR-TOPIC` in a browser and you should see
> messages arrive on your phone.

### 2. Put this project in a GitHub repo

1. Create a **new repository** on GitHub (private is fine).
2. Upload these files (keep the folder structure):
   ```
   check_sit_housing.py
   .github/workflows/monitor.yml
   .gitignore
   README.md
   ```
   You can drag-and-drop them via GitHub's "Add file → Upload files", or push with
   git if you prefer.

### 3. Add your ntfy topic as a repository secret

In your repo: **Settings → Secrets and variables → Actions → New repository secret**
- **Name:** `NTFY_TOPIC`
- **Value:** your topic from step 1 (e.g. `sit-trondheim-7hK2pQ9x`)

(Optional) To watch a different city instead of Trondheim, add a **variable**
(same page, "Variables" tab) named `CITY` with a value like `Ålesund` or `Gjøvik`.

### 4. Turn it on

1. Go to the **Actions** tab. If prompted, click **"I understand my workflows,
   enable them"**.
2. Open **"Sit housing monitor"** → **Run workflow** to trigger it once manually.
3. Within a minute you should get a **"Sit monitor started"** notification listing
   the units currently available in Trondheim. That confirms everything works.

From then on it runs automatically every ~10 minutes and only pings you when a
**new** unit appears.

---

## Good to know

- **First run** sends one summary notification (baseline) and does **not** spam you
  for every already-listed unit. After that, only genuinely new units notify.
- **State** is stored in `state/seen.json`, which the workflow commits back to the
  repo after each run. That's how it remembers across runs.
- **No duplicate pings:** listings flicker (a unit briefly drops out of the API or
  gets reserved, then reappears). The script keeps a persistent ledger and will
  **not** re-notify about the same unit within a cooldown window (default 72 h),
  so you're only pinged for genuinely new listings. Tune with the `RENOTIFY_HOURS`
  repository variable if you want a shorter/longer window.
- **Timing:** GitHub's scheduler honours a ~5-minute minimum and can be delayed a
  few minutes under load. Change the `cron:` line in
  `.github/workflows/monitor.yml` if you want a different cadence.
- **60-day sleep:** GitHub disables scheduled workflows in a repo with no commits
  for 60 days. This monitor commits its state whenever listings change, which keeps
  it alive; if Trondheim is quiet for 60 days, just push any commit (or click "Run
  workflow") to wake it.
- **Notifications stop?** Check the Actions tab for failed runs, and confirm the
  `NTFY_TOPIC` secret matches the topic you're subscribed to on your phone.

## Run it locally (optional)

You don't need to, but to test on your own machine (Python 3.9+):

```bash
# PowerShell
$env:NTFY_TOPIC = "sit-trondheim-7hK2pQ9x"
python check_sit_housing.py
```

The first local run creates `state/seen.json`; delete it to reset the baseline.
