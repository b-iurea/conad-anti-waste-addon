"""Configuration — every setting comes from the environment (12-factor).

Defaults are tuned for local WSL development; in Kubernetes everything lands
under /data and comes from a ConfigMap + Secret.
"""

from pathlib import Path
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PARENT_PROJECT = PROJECT_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage -----------------------------------------------------------
    data_dir: Path = PROJECT_ROOT / "data"
    # The browser login lives in the parent project and writes its cookies there;
    # we read the same file rather than keeping a second session.
    conad_cookies_path: Path = PARENT_PROJECT / "data" / "sessions" / "cookies.json"
    # Historical export used to seed the inventory without hitting the network.
    orders_csv_path: Path = PARENT_PROJECT / "data" / "orders.csv"

    # --- conad login -------------------------------------------------------
    # Used to re-authenticate automatically when the session dies. The login
    # runs a real Chrome on a virtual display: headless is detected and refused
    # by my.conad.it's bot protection (verified — see app/conad_login.py).
    conad_email: str = ""
    conad_password: str = ""
    # Persistent browser profile. Browsing history accumulates here and
    # measurably improves the reCAPTCHA Enterprise score, so it belongs on the
    # volume, not in the image.
    chrome_profile_dir: Optional[Path] = None
    # Forcing True will make login fail; exposed only for experiments.
    login_headless: bool = False
    # Deliberately 1. The captcha is score-based and reputational: repeated
    # attempts in a short window measurably *lower* the score, so an immediate
    # retry makes the next one likelier to fail too. Better to fail, wait out
    # the cooldown, and tell a human.
    login_max_attempts: int = 1
    # Hours to wait after a failed login before trying again.
    login_cooldown_hours: int = 6
    xvfb_display: str = ":99"
    xvfb_screen: str = "1440x900x24"

    # --- telegram ----------------------------------------------------------
    telegram_bot_token: str = ""
    # Whitelist. The bot ignores every chat that is not listed here.
    telegram_allowed_chat_ids: str = ""

    # --- scheduling --------------------------------------------------------
    tz: str = "Europe/Rome"
    daily_prompt_hour: str = "20:30"
    weekly_reckoning_day: str = "sun"
    weekly_reckoning_hour: str = "18:00"
    refresh_interval_hours: int = 24
    expiry_warn_days: int = 2
    max_nag_level: int = 3

    # --- web ---------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    scan_token: str = ""

    # --- intelligence (Phase 8) --------------------------------------------
    intelligence: Literal["rules", "deepseek"] = "rules"
    deepseek_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "inventory.db"

    @property
    def profile_dir(self) -> Path:
        return self.chrome_profile_dir or (self.data_dir / "sessions" / "chrome-profile")

    @property
    def can_auto_login(self) -> bool:
        return bool(self.conad_email and self.conad_password)

    @property
    def allowed_chat_ids(self) -> set[int]:
        raw = self.telegram_allowed_chat_ids.replace(";", ",")
        return {int(p) for p in (x.strip() for x in raw.split(",")) if p.lstrip("-").isdigit()}

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings
