"""
Conad Authentication Module
===========================
This module provides methods for authenticating and managing sessions with my.conad.it.

Technical Note on my.conad.it Authentication:
---------------------------------------------
my.conad.it login requires a dynamic `protectionToken` generated client-side via JavaScript 
(using `@conad-common/clientlib-global-protection` and `BotD` fingerprinting). 

When performing requests via pure HTTP (e.g., requests/httpx), direct authentication without 
a protection token or browser context will be rejected by the server.

This file provides:
1. `ConadHttpSession`: A requests.Session wrapper for session persistence and API requests using saved cookies.
2. `attempt_direct_login()`: Pure HTTP request attempt demonstrating the required API structure.
3. `load_saved_session()` / `save_session_cookies()`: Cookie persistence helpers.
"""

import json
import os
import requests
from pathlib import Path
from typing import Optional, Dict, Any

SESSION_FILE = Path(__file__).parent.parent / "data" / "sessions" / "cookies.json"
LOGIN_URL = "https://my.conad.it/login"
LOGIN_API_ENDPOINT = "https://my.conad.it/api/myconad/it-it.login.json"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://my.conad.it/login",
    "Origin": "https://my.conad.it",
}


class ConadHttpSession:
    """Manages HTTP session state and cookies for Conad API interaction."""
    
    def __init__(self, cookies_file: Path = SESSION_FILE):
        self.cookies_file = cookies_file
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.load_cookies()

    def load_cookies(self) -> bool:
        """Loads saved session cookies from disk if available."""
        if not self.cookies_file.exists():
            return False
        try:
            with open(self.cookies_file, "r", encoding="utf-8") as f:
                cookies_dict = json.load(f)
                self.session.cookies.update(cookies_dict)
            return True
        except Exception as e:
            print(f"[!] Error loading session cookies: {e}")
            return False

    def save_cookies(self) -> None:
        """Saves current session cookies to disk."""
        self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
        cookies_dict = self.session.cookies.get_dict()
        try:
            with open(self.cookies_file, "w", encoding="utf-8") as f:
                json.dump(cookies_dict, f, indent=2)
            print(f"[+] Cookies saved to {self.cookies_file}")
        except OSError as e:
            print(f"[!] Error saving cookies: {e}")

    def is_authenticated(self) -> bool:
        """Checks if the session is currently authenticated against Conad."""
        try:
            response = self.session.get("https://my.conad.it/i-miei-ordini", allow_redirects=False)
            # If 200 OK and not redirected to /login, session is valid
            ok = response.status_code == 200 and "/login" not in response.url
        except requests.RequestException as e:
            print(f"[!] Session check failed ({e}); session considered invalid.")
            ok = False
        if not ok:
            # Deliberately no "run this command": inside the add-on there is no
            # shell to run it in, and the module this used to name (src.
            # auth_interactive) is not even in the image. Re-login is automatic
            # — app.auth_auto.ensure_session() drives app.conad_login. If it
            # keeps failing it is the captcha score, not a missing command:
            # see DOCS.md, "Il primo login è la parte delicata".
            print("[!] Session expired or invalid — automatic re-login will be attempted.")
        return ok


def attempt_direct_login(email: str, password: str, protection_token: Optional[str] = None) -> Dict[str, Any]:
    """
    Attempts direct HTTP authentication to Conad.
    
    Note: If protection_token is None, the server will likely return an authentication error 
    due to client-side BotD anti-bot requirements.
    """
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    # Step 1: Initial GET to acquire initial cookies (affinity, session, CSRF)
    try:
        init_res = session.get(LOGIN_URL)
        init_res.raise_for_status()
    except requests.RequestException as e:
        return {"success": False, "error": f"Failed to load login page: {e}"}

    # Step 2: Construct the login payload expected by my.conad.it frontend
    payload = {
        "email": email,
        "password": password,
        "mantain": True,
        "cb": "/login",
        "protectionToken": protection_token or ""
    }

    # Step 3: POST request to submit credentials
    try:
        response = session.post(LOGIN_API_ENDPOINT, json=payload)
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text
        
        if response.status_code == 200:
            # Save session cookies if login succeeds
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            cookies_dict = session.cookies.get_dict()
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies_dict, f, indent=2)
            return {"success": True, "data": data, "cookies": cookies_dict}
        else:
            return {
                "success": False, 
                "status_code": response.status_code, 
                "data": data,
                "note": "my.conad.it requires dynamic JavaScript protectionToken (BotD/reCAPTCHA)."
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    
    print("=========================================================")
    print("       CONAD DIRECT HTTP AUTHENTICATION TEST             ")
    print("=========================================================")
    
    user_email = sys.argv[1] if len(sys.argv) > 1 else os.getenv("CONAD_EMAIL")
    user_pass = sys.argv[2] if len(sys.argv) > 2 else os.getenv("CONAD_PASSWORD")
    token = sys.argv[3] if len(sys.argv) > 3 else None
    
    if user_email and user_pass:
        print(f"Testing direct HTTP POST for: {user_email}")
        result = attempt_direct_login(user_email, user_pass, token)
        print(json.dumps(result, indent=2))
    else:
        print("Usage: python -m src.auth [email] [password] [protection_token]")
        print("Or configure CONAD_EMAIL and CONAD_PASSWORD in .env")
