"""Configuration loaded from environment / .env."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Server
    app_base_url: str = "http://localhost:8080"
    port: int = 8080
    log_level: str = "INFO"

    # Bot Framework
    bot_app_id: str = ""
    bot_app_password: str = ""
    bot_tenant_id: str = "common"
    bot_app_type: str = "SingleTenant"  # SingleTenant | MultiTenant | UserAssignedMSI

    # Azure OpenAI
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = "gpt-4o"
    azure_openai_api_version: str = "2024-10-21"

    # Azure Communication Services
    acs_connection_string: str = ""
    acs_sender_address: str = ""

    # Demo personas
    demo_user_email: str = "james.nguyen@microsoft.com"
    demo_user_tenant_id: str = ""
    demo_user_aad_object_id: str = ""  # for proactive auto-create of Teams chat
    demo_admin_email: str = "james.nguyen@microsoft.com"
    demo_manager_email: str = "james.nguyen@microsoft.com"

    # Bot Framework Connector service URL — used for proactive createConversation
    # when no ConversationReference is already stored. Default works for any tenant.
    bot_service_url: str = "https://smba.trafficmanager.net/amer/"

    # Actionable email
    oam_originator_id: str = ""
    oam_app_id_uri: str = ""
    oam_entra_app_id: str = ""

    # Signed token
    cta_token_secret: str = "dev-only-secret-change-me"

    # Flags
    enable_demo_console: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
