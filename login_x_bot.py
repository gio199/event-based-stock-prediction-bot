"""One-time manual login helper for the X/Twitter Event Feed source.

Run this once (`python login_x_bot.py`) with a real, visible browser
window. Log into the dedicated bot account yourself - this lets you
handle any CAPTCHA/2FA challenge as a human, which the unattended
background poller (news_sources.XMuskSource) cannot do. Once logged in,
come back to this terminal and press Enter; the session is saved to
x_session_state.json so the background poller can reuse it headlessly.

Treat the saved session file as sensitive as a password - anyone holding
it can act as your logged-in session. It lives in the gitignored `data/`
directory, but don't share it.
"""
import sys

from app_util import configure_logging, load_env_once
from news_sources import X_MUSK_PROFILE_URL, X_SESSION_STATE_FILE


def main():
    configure_logging()
    load_env_once()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright is not installed.")
        print("Run: pip install -r requirements.txt && playwright install chromium")
        sys.exit(1)

    print("=" * 70)
    print("X/Twitter bot login")
    print("=" * 70)
    print("A browser window will open. Log into your DEDICATED BOT ACCOUNT")
    print("(not your personal account) - this is the account that will be")
    print("used to check Elon Musk's profile periodically.")
    print()
    input("Press Enter to open the browser...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://x.com/login")

        print()
        print("Log in now in the opened browser window.")
        print(f"Once logged in, optionally visit {X_MUSK_PROFILE_URL} to confirm it loads.")
        input("Once you're fully logged in, come back here and press Enter to save the session...")

        context.storage_state(path=X_SESSION_STATE_FILE)
        browser.close()

    print()
    print(f"[OK] Session saved to: {X_SESSION_STATE_FILE}")
    print("You can now enable the 'x_musk' source from the Event Feed tab's Settings panel.")
    print()
    print("NOTE: treat this file like a password. If X ever logs the bot account out")
    print("(e.g. after a long period, or a security check), rerun this script.")


if __name__ == "__main__":
    main()
