# NYC marriage ceremony slot checker

Checks https://clerkscheduler.cityofnewyork.us/s/MarriageCeremony every 15 minutes
for a **Manhattan** office slot on **Tue Oct 20, 2026** at **9:00 AM or later**, and
sends you a Telegram message and/or email when one opens up. You get one alert per
new slot, not a repeat every 15 minutes.

## 1. Set up Telegram (about 2 minutes)

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts.
   Copy the **bot token** it gives you (looks like `123456:ABC-...`).
2. Open a chat with your new bot and send it any message (e.g. "hi").
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
   `"chat":{"id": ...}`. That number is your **chat ID**.

Email (optional, works alongside Telegram): use a Gmail address plus an
[App Password](https://myaccount.google.com/apppasswords) (needs 2-step verification).

## 2a. Run it for free on GitHub Actions (no computer needed)

1. Create a **private** GitHub repo and push this folder to it.
2. Repo → Settings → Secrets and variables → Actions → *New repository secret*:
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - (optional) `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`
3. Go to the Actions tab → **Check NYC marriage slots** → **Run workflow** to test it.
   Open the run to see the log ("Times seen on page ...") and download the
   `debug-*` artifact to see screenshots of each step.
4. After that it runs every ~15 min on its own. GitHub can delay scheduled runs by a few minutes.
   Disable the workflow after Oct 20.

## 2b. Or run it on your own computer

```bash
pip install -r requirements.txt
python -m playwright install chromium
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python checker.py --test-notify   # check that notifications arrive
HEADFUL=1 python checker.py       # one check with a visible browser, to watch it work
python checker.py --loop          # check every 15 minutes until you stop it
```

## Settings (environment variables)

| Variable      | Default      | Meaning                                  |
|---------------|--------------|------------------------------------------|
| `TARGET_DATE` | `2026-10-20` | Date to check                            |
| `OFFICE`      | `Manhattan`  | Office name as shown on the site         |
| `MIN_TIME`    | `09:00`      | Earliest slot time that counts (24h, inclusive) |

## How it works / if it breaks

The site is a Salesforce (Lightning) app that renders with JavaScript, so the checker
drives a real headless Chromium browser: it selects the office, opens the date, and reads
every time like `9:15 AM` shown after the date is picked. Times already on the page before
then, like office hours, are ignored. It doesn't depend on exact CSS selectors, but if the
site's flow is different (for example, an extra "Next" step or a captcha), the run fails
with an error like `Could not select date`. The screenshots in `debug/` (or the
GitHub `debug-*` artifact) show the step where it stopped.
