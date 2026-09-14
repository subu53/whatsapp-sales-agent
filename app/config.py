"""Central configuration. All values are overridable via environment variables
or a local .env file (see .env.example) — never hardcode secrets in code."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM ---
    llm_provider: str = "anthropic"  # "anthropic" | "deepseek"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    max_tool_iterations: int = 6
    max_reply_tokens: int = 700

    # --- Per-contact rate limit (protects the LLM bill from one runaway sender) ---
    rate_limit_max_messages: int = 20
    rate_limit_window_seconds: int = 3600

    # --- Twilio / WhatsApp ---
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_number: str = "whatsapp:+14155238886"
    twilio_validate_signature: bool = True

    # --- Meta WhatsApp Cloud API (direct) ---
    meta_access_token: str = ""
    meta_phone_number_id: str = ""
    meta_webhook_verify_token: str = ""
    meta_app_secret: str = ""  # optional — enables X-Hub-Signature-256 validation
    meta_graph_api_version: str = "v23.0"

    # --- Business config ---
    business_name: str = "Alpha Fitness"
    business_whatsapp: str = "+254718040612"
    business_website: str = "https://alphafitness.co.ke"
    human_escalation_whatsapp: str = ""

    # --- Storage ---
    database_path: str = "data/leads.db"
    catalogue_path: str = "data/catalogue/products.json"
    site_info_path: str = "data/site_info.json"

    # --- Safety ---
    allowed_fetch_domain: str = "alphafitness.co.ke"


settings = Settings()
