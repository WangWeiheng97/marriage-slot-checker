#!/usr/bin/env python3
"""
NYC City Clerk marriage-ceremony slot checker.

Opens https://clerkscheduler.cityofnewyork.us/s/MarriageCeremony in headless
Chromium, picks the Manhattan office and the target date, reads the available
time slots, and sends a Telegram and/or email notification when a slot at or
after MIN_TIME shows up.

Config (environment variables):
  TARGET_DATE        2026-10-20
  OFFICE             Manhattan
  MIN_TIME           08:00          (24h; slots >= this time count)
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID           -> Telegram notification
  SMTP_USER / SMTP_PASSWORD / EMAIL_TO            -> email (Gmail app password)
  SMTP_HOST (smtp.gmail.com) / SMTP_PORT (465)
  STATE_FILE         state.json     (avoids re-sending the same slots)
  NOTIFY_EVERY_RUN   0              (1 = alert on every check while slots are open)
  DEBUG_DIR          debug          (screenshots / page text for troubleshooting)
  BROWSER_CHANNEL    (unset)        "chrome" = use the installed Google Chrome

Usage:
  python checker.py            # check once
  python checker.py --loop     # check every CHECK_INTERVAL_MIN (5) minutes
                               # for MAX_RUNTIME_MIN (forever if unset)
  python checker.py --test-notify   # send a test notification and exit
"""

import datetime as dt
import json
import os
import re
import smtplib
import sys
import time
from collections import Counter
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

URL = os.environ.get("URL", "https://clerkscheduler.cityofnewyork.us/s/MarriageCeremony")
TARGET_DATE = dt.date.fromisoformat(os.environ.get("TARGET_DATE", "2026-10-20"))
OFFICE = os.environ.get("OFFICE", "Manhattan")
MIN_TIME = dt.time.fromisoformat(os.environ.get("MIN_TIME", "08:00"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
DEBUG_DIR = Path(os.environ.get("DEBUG_DIR", "debug"))
INTERVAL_SECONDS = float(os.environ.get("CHECK_INTERVAL_MIN", "5")) * 60
MAX_RUNTIME_SECONDS = float(os.environ.get("MAX_RUNTIME_MIN", "0")) * 60  # 0 = no limit
# 1 = alert on every check while slots are open; 0 = only when new slots appear
NOTIFY_EVERY_RUN = os.environ.get("NOTIFY_EVERY_RUN", "0") == "1"

TIME_RE = re.compile(r"(?<!\d)(1[0-2]|0?[1-9]):([0-5]\d)\s*([AaPp])\.?\s*[Mm]\.?")

# Collects text from the whole page, including inside (Lightning) shadow roots.
DEEP_TEXT_JS = """
() => {
  const out = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot);
    }
    if (root.innerText !== undefined) out.push(root.innerText);
    else for (const n of root.childNodes) out.push(n.textContent || '');
  };
  walk(document.body);
  return out.join('\\n');
}
"""


# Multi-day layouts: find every date header ("Tue 10/20", "Oct 20", ...), then
# give each time on the page to the nearest header above it (by x position),
# and return only the times under the target date's header.
COLUMN_TIMES_JS = """
([targetRe, anyDayRe, timeRe]) => {
  const els = [];
  const walk = (root) => {
    for (const e of root.querySelectorAll('*')) { els.push(e); if (e.shadowRoot) walk(e.shadowRoot); }
  };
  walk(document);
  const text = (e) => (e.innerText || '').trim();
  const box = (e) => e.getBoundingClientRect();
  const visible = (e) => { const r = box(e); return r.width > 0 && r.height > 0; };
  // Innermost visible short elements whose text matches re
  const innermost = (re, maxLen) => {
    const m = els.filter(e => visible(e) && text(e).length <= maxLen && re.test(text(e)));
    return m.filter(e => !m.some(o => o !== e && e.contains(o)));
  };
  const target = new RegExp(targetRe, 'i'), anyDay = new RegExp(anyDayRe, 'i');
  const headers = innermost(anyDay, 40).filter(e => !new RegExp(timeRe, 'i').test(text(e)));
  const isTarget = (h) => target.test(text(h));
  const times = innermost(new RegExp(timeRe, 'i'), 40);
  const picked = [];
  for (const t of times) {
    const tb = box(t), cx = tb.left + tb.width / 2;
    let best = null, bestDist = Infinity;
    for (const h of headers) {
      const hb = box(h);
      if (hb.bottom > tb.top + 2) continue;          // header must be above the time
      const d = Math.abs(hb.left + hb.width / 2 - cx);
      if (d < bestDist) { bestDist = d; best = h; }
    }
    if (best && isTarget(best)) picked.push(text(t));
  }
  return { headers: headers.map(text), targetFound: headers.some(isTarget), times: picked };
}
"""


def date_regexes(d):
    """(regex for this date's column header, regex for any date header)."""
    mon = d.strftime("%b").lower()
    mon_re = rf"{mon}[a-z]*\.?"
    wd_re = rf"{d.strftime('%a').lower()}[a-z]*\.?"
    target = (rf"\b({mon_re}\s+{d.day}\b|{d.month}/{d.day}\b|0?{d.month}/0?{d.day}\b|"
              rf"{wd_re}\W+({mon_re}\s+)?{d.day}\b|{d.day}\s+{mon_re})")
    any_day = (r"\b((jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|"
               r"\d{1,2}/\d{1,2}\b|(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\W+"
               r"((jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+)?\d{1,2}\b)")
    return target, any_day


def log(msg):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30)
    return True


def send_telegram_file(path, caption):
    """Sends a file (e.g. the page screenshot) as a Telegram document."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    path = Path(path)
    if not (token and chat and path.exists()):
        return False
    boundary = "----slotchecker" + str(int(time.time() * 1000))
    parts = []
    for name, value in (("chat_id", chat), ("caption", caption)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
                 f"Content-Type: image/png\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=60)
    return True


def send_email(subject, body):
    user, pw, to = (os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))
    if not (user and pw and to):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
    return True


def notify(subject, body):
    sent = []
    for name, fn in (("telegram", lambda: send_telegram(f"{subject}\n\n{body}")),
                     ("email", lambda: send_email(subject, body))):
        try:
            if fn():
                sent.append(name)
        except Exception as e:  # keep going if one channel fails
            log(f"{name} notification failed: {e}")
    if not sent:
        log("WARNING: no notification channel configured / delivered")
    else:
        log(f"Notified via {', '.join(sent)}")
    return bool(sent)


# --------------------------------------------------------------------------- #
# Page interaction helpers (selector-agnostic, the Salesforce markup changes)
# --------------------------------------------------------------------------- #
def try_click(page, locators, timeout=3000):
    for loc in locators:
        try:
            target = loc.first
            target.wait_for(state="visible", timeout=timeout)
            target.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


def settle(page, ms=1500):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    page.wait_for_timeout(ms)


DROPDOWN_SEL = "select, [role='combobox'], lightning-combobox, lightning-select"


def _shows_office(cb):
    """True if a dropdown currently displays the office as its selected value."""
    name = re.compile(OFFICE, re.I)
    for get in (lambda: cb.input_value(timeout=1000), lambda: cb.inner_text(timeout=1000),
                lambda: cb.get_attribute("data-value") or "", lambda: cb.get_attribute("aria-label") or "",
                lambda: cb.evaluate("e => e.closest('lightning-combobox, .slds-form-element')"
                                    "?.innerText || ''")):
        try:
            if name.search(get() or ""):
                return True
        except Exception:
            pass
    return False


def choose_office(page):
    """Selects OFFICE in the office dropdown. Never clicks loose text on the page."""
    name = re.compile(OFFICE, re.I)
    try:  # the dropdown can render a few seconds after the page
        page.locator(DROPDOWN_SEL).first.wait_for(state="visible", timeout=30000)
    except PWTimeout:
        return False

    # A native <select>
    for sel in page.locator("select").all():
        try:
            opts = sel.locator("option").all_inner_texts()
            match = next((o for o in opts if name.search(o)), None)
            if match:
                sel.select_option(label=match)
                return True
        except Exception:
            pass

    # A Lightning / custom combobox: open it, click the matching option, verify.
    for cb in page.get_by_role("combobox").all():
        try:
            if not cb.is_visible():
                continue
            cb.scroll_into_view_if_needed(timeout=2000)
            cb.click(timeout=3000)
            page.wait_for_timeout(800)
            option = page.get_by_role("option", name=name).or_(
                page.locator("lightning-base-combobox-item, [role='listbox'] li").filter(has_text=name))
            if not option.count():
                # Some comboboxes are searchable: type the name to filter the list.
                try:
                    cb.fill(OFFICE, timeout=1500)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
            if option.count():
                option.first.click(timeout=3000)
                page.wait_for_timeout(800)
                if _shows_office(cb):
                    return True
                log("Clicked the office option but the dropdown doesn't show it; trying next dropdown")
            page.keyboard.press("Escape")
        except Exception as e:
            log(f"combobox attempt failed: {e}")
    return False


def click_next(page):
    return try_click(page, [
        page.get_by_role("button", name=re.compile(r"^\s*(next|continue|search|check availability)\s*$", re.I)),
    ], timeout=1500)


def choose_date(page):
    d = TARGET_DATE
    # 1) A date <input> we can type into.
    inputs = page.locator("input[type='date'], input[name*='date' i], input[placeholder*='date' i], "
                          "input[aria-label*='date' i], lightning-datepicker input")
    for i in range(inputs.count()):
        inp = inputs.nth(i)
        try:
            if not inp.is_visible():
                continue
            is_native = (inp.get_attribute("type") or "").lower() == "date"
            for value in ([d.isoformat()] if is_native else
                          [d.strftime("%b %-d, %Y"), d.strftime("%m/%d/%Y"), d.isoformat()]):
                inp.fill(value)
                inp.press("Enter")
                inp.blur()
                settle(page, 1000)
                if not page.locator("[aria-invalid='true']").count():
                    return True
        except Exception:
            continue

    # 2) A calendar widget: navigate to the right month, click the day.
    month_label = re.compile(rf"{d.strftime('%B')}\s+{d.year}", re.I)
    next_month = [page.get_by_role("button", name=re.compile(r"next\s*month|next", re.I)),
                  page.locator("[title*='Next Month' i], [aria-label*='Next Month' i]")]
    for _ in range(14):
        text = page.evaluate(DEEP_TEXT_JS)
        if month_label.search(text):
            break
        if not try_click(page, next_month, timeout=1500):
            break
        page.wait_for_timeout(700)

    day_names = [
        d.strftime("%A, %B %-d, %Y"),
        d.strftime("%B %-d, %Y"),
        d.strftime("%b %-d, %Y"),
        d.strftime("%A, %B %-d"),
        d.isoformat(),
    ]
    locs = []
    for n in day_names:
        locs += [page.locator(f"[aria-label*='{n}' i]"), page.locator(f"[data-value='{n}']"),
                 page.locator(f"[data-date='{n}']"), page.locator(f"[title*='{n}' i]")]
    locs += [
        page.get_by_role("gridcell", name=re.compile(rf"^\s*{d.day}\s*$")),
        page.get_by_role("button", name=re.compile(rf"^\s*{d.day}\s*$")),
        page.locator("td, [role='gridcell'] span, .slds-day").filter(
            has_text=re.compile(rf"^\s*{d.day}\s*$")),
    ]
    return try_click(page, locs, timeout=1500)


def parse_times(text):
    """Counter of times found in text, e.g. {time(9, 30): 1}."""
    found = Counter()
    for h, m, ap in TIME_RE.findall(text):
        h = int(h) % 12 + (12 if ap.lower() == "p" else 0)
        found[dt.time(h, int(m))] += 1
    return found


def dump_debug(page, tag):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{tag}.png"), full_page=True)
        (DEBUG_DIR / f"{tag}.txt").write_text(page.evaluate(DEEP_TEXT_JS))
        (DEBUG_DIR / f"{tag}.html").write_text(page.content())
    except Exception as e:
        log(f"debug dump failed: {e}")


# --------------------------------------------------------------------------- #
# Main check
# --------------------------------------------------------------------------- #
def check():
    """Returns the list of qualifying slot times (as 'H:MM AM' strings)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=os.environ.get("HEADFUL") != "1",
                                    executable_path=os.environ.get("CHROMIUM_PATH") or None,
                                    channel=os.environ.get("BROWSER_CHANNEL") or None)
        page = browser.new_page(viewport={"width": 1280, "height": 1800})

        # Keep the JSON the page fetches (Salesforce aura/apex calls): slot times
        # often show up there even when the visual widget is awkward to read.
        responses = []

        def on_response(resp):
            try:
                if "json" in (resp.headers.get("content-type") or "") or "aura" in resp.url:
                    responses.append(resp.text())
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            settle(page, 3000)
            dump_debug(page, "1_loaded")

            if not choose_office(page):
                dump_debug(page, "error_office")
                raise RuntimeError(f"Could not select '{OFFICE}' in the office dropdown (see {DEBUG_DIR}/)")
            settle(page)
            click_next(page) and settle(page)
            dump_debug(page, "2_office")

            responses.clear()  # only keep data fetched for the chosen date
            # Times already on the page before picking a date (office hours etc.)
            baseline = parse_times(page.evaluate(DEEP_TEXT_JS))
            if not choose_date(page):
                dump_debug(page, "error_date")
                raise RuntimeError(f"Could not select date {TARGET_DATE} (see {DEBUG_DIR}/)")
            settle(page, 2500)
            click_next(page) and settle(page, 2500)
            dump_debug(page, "3_date")

            page_text = page.evaluate(DEEP_TEXT_JS)
            target_re, any_day_re = date_regexes(TARGET_DATE)
            columns = page.evaluate(COLUMN_TIMES_JS, [target_re, any_day_re, TIME_RE.pattern])
        finally:
            browser.close()

    no_slots = re.search(r"no (available )?(appointments|slots|times)|fully booked|not available",
                         page_text, re.I)
    if len(columns["headers"]) >= 2:
        # Several days on screen: only trust times under the target date's column.
        log(f"Date columns on page: {columns['headers']}")
        if not columns["targetFound"]:
            raise RuntimeError(f"No column for {TARGET_DATE:%a %b %-d} on the page (see {DEBUG_DIR}/)")
        times = sorted(parse_times("\n".join(columns["times"])))
    else:
        times = sorted(parse_times(page_text) - baseline)
    log(f"Times seen on page for {TARGET_DATE}: {[t.strftime('%-I:%M %p') for t in times] or 'none'}"
        + (" (page says no availability)" if no_slots else ""))
    return [t.strftime("%-I:%M %p") for t in times if t >= MIN_TIME]


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def run_once():
    if dt.date.today() > TARGET_DATE:
        log(f"{TARGET_DATE} has passed; nothing to check. You can disable the schedule.")
        return []
    slots = check()
    state = load_state()
    previous = set(state.get("slots", []))
    new = [s for s in slots if s not in previous]

    if new or (slots and NOTIFY_EVERY_RUN):
        subject = f"NYC marriage slot open: {OFFICE} {TARGET_DATE:%a %b %-d}"
        body = (f"Available time(s) at/after {MIN_TIME:%-I:%M %p}: {', '.join(slots)}\n"
                f"New since last check: {', '.join(new) or 'none'}\n\nBook now: {URL}")
        log(subject + " -> " + ", ".join(slots))
        delivered = notify(subject, body)
        try:  # show exactly what the checker saw, so the result can be verified
            send_telegram_file(DEBUG_DIR / "3_date.png", "What the checker saw (after picking office + date)")
        except Exception as e:
            log(f"telegram screenshot failed: {e}")
        if not delivered:
            # Don't remember slots we failed to tell you about; retry next run.
            slots = [s for s in slots if s in previous]
    elif slots:
        log(f"Slots still open (already notified): {', '.join(slots)}")
    else:
        log("No qualifying slots.")

    STATE_FILE.write_text(json.dumps({"slots": slots, "checked_at": dt.datetime.now().isoformat()}))
    return slots


def main():
    if "--test-notify" in sys.argv:
        notify("Test: NYC marriage slot checker", f"Notifications work. Watching {OFFICE} on {TARGET_DATE}.")
        return
    if "--loop" in sys.argv:
        start, failures = time.monotonic(), 0
        while dt.date.today() <= TARGET_DATE:
            began = time.monotonic()
            try:
                run_once()
                failures = 0
            except Exception as e:
                failures += 1
                log(f"Check failed ({failures} in a row): {e}")
                if failures == 3:  # warn once, not on every failure
                    notify("NYC slot checker is failing",
                           f"The last 3 checks failed, it may need fixing.\nLast error: {e}")
                    shots = sorted(DEBUG_DIR.glob("error_*.png"), key=lambda f: f.stat().st_mtime)
                    try:
                        if shots:
                            send_telegram_file(shots[-1], "Where the checker got stuck")
                    except Exception as e2:
                        log(f"telegram screenshot failed: {e2}")
            next_at = began + INTERVAL_SECONDS
            if MAX_RUNTIME_SECONDS and next_at - start > MAX_RUNTIME_SECONDS:
                log("Reached MAX_RUNTIME_MIN, exiting.")
                return
            time.sleep(max(0, next_at - time.monotonic()))
        return
    run_once()


if __name__ == "__main__":
    main()
