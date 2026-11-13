from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Gemini ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # --- Azure OpenAI (orchestrator fallback) ---
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment_orchestrator: str = "gpt-4o"
    azure_openai_deployment_stage: str = "gpt-4o-mini"
    azure_openai_api_version: str = "2024-08-01-preview"

    # --- Azure AI Search ---
    azure_search_endpoint: str = ""
    azure_search_api_key: str = ""
    azure_search_index_name: str = "dact-knowledge"

    log_level: str = "INFO"
    env: str = "local"


settings = Settings()
