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
    # Defaults describe a sample business so the repo runs out of the box.
    # A live deployment sets the real values as environment variables
    # (App Settings on Azure) and keeps the business's files outside the repo.
    business_name: str = "Sample Fitness Supply"
    business_whatsapp: str = "+254700000000"
    business_website: str = "https://www.example.com"
    human_escalation_whatsapp: str = ""
    operator_name: str = "Esubu Tech"
    privacy_contact_email: str = "subusam5@gmail.com"

    # --- Storage ---
    database_path: str = "data/leads.db"
    catalogue_path: str = "data/catalogue/products.json"
    site_info_path: str = "data/site_info.json"
    # Empty uses the bundled sample prompt at app/agent/prompts/system_prompt.md.
    system_prompt_path: str = ""

    # --- Safety ---
    allowed_fetch_domain: str = "example.com"
    # /debug/* routes return 404 unless this is set AND sent as X-Debug-Token.
    debug_token: str = ""


settings = Settings()
