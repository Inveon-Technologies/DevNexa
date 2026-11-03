from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every value can be overridden with an env var prefixed DEVNEXA_."""

    model_config = SettingsConfigDict(env_prefix="DEVNEXA_", env_file=".env", extra="ignore")

    app_name: str = "DevNexa"
    database_url: str = "sqlite:///./devnexa.db"
    cors_origins: str = "*"

    # "simulated" runs everything against an in-memory model of the sample app (no Docker needed).
    # "live" talks to the real sample app, Prometheus, Loki, Jaeger and Docker from docker-compose.
    environment_mode: str = "simulated"
    # Seconds each simulated pipeline/incident step sleeps so the dashboard can show progress.
    sim_step_delay: float = 0.8
    seed_demo_data: bool = True

    # LLM: "heuristic" (offline, deterministic evidence scorer), "anthropic", or "openai"
    # ("openai" also covers OpenAI-compatible local servers such as Ollama or vLLM via llm_base_url).
    llm_provider: str = "heuristic"
    llm_model: str = "claude-sonnet-5-5"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_timeout: float = 60.0

    embedding_dim: int = 384
    rag_top_k: int = 4
    rag_min_similarity: float = 0.25
    max_reanalysis_attempts: int = 2

    # Live integrations (docker-compose service names by default)
    sample_app_url: str = "http://sample-app:8000"
    prometheus_url: str = "http://prometheus:9090"
    loki_url: str = "http://loki:3100"
    jaeger_url: str = "http://jaeger:16686"
    sample_app_container: str = "devnexa-sample-app"
    sample_app_image: str = "devnexa/sample-app"
    # Live pipelines build and test this checkout (or the pushed commit's `sample_app_subdir` when GitHub triggered it).
    sample_app_src: str = ""
    sample_app_subdir: str = "sample-app"

    github_webhook_secret: str = ""
    github_token: str = ""
    github_repo: str = ""

    # Sign in with GitHub (an OAuth app). The callback URL to register on GitHub is
    # <public_url>/api/auth/github/callback. Only the GitHub logins in github_allowed_users may sign in;
    # if that list is empty, the first account that signs in becomes the owner and nobody else can.
    github_client_id: str = ""
    github_client_secret: str = ""
    github_allowed_users: str = ""
    public_url: str = ""
    session_secret: str = ""
    session_days: int = 14

    # Deploying user repos ("Deploy from GitHub"). The admin token guards the GitHub and app-deploy APIs:
    # in live mode they refuse to work until it is set, because a deploy runs arbitrary code on the host.
    admin_token: str = ""
    # Apps are served at https://<app>.<apps_domain> through Caddy. Empty: publish a host port instead.
    apps_domain: str = ""
    apps_public_host: str = "localhost"
    apps_host_port_base: int = 20000
    apps_network: str = "devnexa_default"
    apps_workdir: str = "/tmp/devnexa-apps"
    apps_memory: str = "512m"
    apps_cpus: str = "1.0"
    apps_build_timeout: int = 900
    apps_health_timeout: int = 60
    apps_monitor_interval: float = 15.0
    apps_max_auto_restarts: int = 3

    # Remote servers (deploy targets reached over SSH). Keys are kept in the database and written here with
    # mode 600 when needed; `servers_ssh_config` (the backend's own ~/.ssh/config) gets an Include line for
    # them so the Docker CLI can reach each server as DOCKER_HOST=ssh://devnexa-server-<id>.
    servers_dir: str = "/tmp/devnexa-servers"
    servers_ssh_config: str = "~/.ssh/config"
    servers_command_timeout: int = 60

    # Live user traffic on the dashboard. "auto": tail Caddy's access log in live mode, simulate it otherwise.
    # "caddy" always tails the log, "simulated" always simulates, "off" disables it.
    traffic_source: str = "auto"
    traffic_log: str = "/var/log/caddy/access.log"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_live(self) -> bool:
        return self.environment_mode == "live"


@lru_cache
def get_settings() -> Settings:
    return Settings()
