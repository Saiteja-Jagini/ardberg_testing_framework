from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{Path(__file__).resolve().parents[2] / '.data' / 'ardberg.db'}")
    data_dir: Path = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parents[2] / ".data"))).resolve()
    github_app_id: str = os.getenv("GITHUB_APP_ID", "")
    github_app_slug: str = os.getenv("GITHUB_APP_SLUG", "")
    github_private_key: str = os.getenv("GITHUB_PRIVATE_KEY", "").replace("\\n", "\n")
    github_webhook_secret: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    github_api_url: str = os.getenv("GITHUB_API_URL", "https://api.github.com")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-6-astra")
    temporal_address: str = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    temporal_task_queue: str = os.getenv("TEMPORAL_TASK_QUEUE", "ardberg-runs")
    frontend_origin: str = os.getenv("FRONTEND_ORIGIN", "http://127.0.0.1:3000")
    public_dashboard_url: str = os.getenv("PUBLIC_DASHBOARD_URL", "")
    docker_image: str = os.getenv("RUNNER_IMAGE", "ardberg-runner:local")
    postgres_test_image: str = os.getenv("POSTGRES_TEST_IMAGE", "postgres:16-alpine")
    postgres_vector_image: str = os.getenv("POSTGRES_VECTOR_IMAGE", "pgvector/pgvector:pg16-trixie")
    runner_memory: str = os.getenv("RUNNER_MEMORY", "5g")
    preview_memory: str = os.getenv("PREVIEW_MEMORY", os.getenv("RUNNER_MEMORY", "5g"))
    runner_cpus: str = os.getenv("RUNNER_CPUS", "4")
    runner_pids_limit: int = int(os.getenv("RUNNER_PIDS_LIMIT", "1024"))
    npm_registry_url: str = os.getenv("NPM_REGISTRY_URL", "https://registry.npmjs.org/")
    pip_index_url: str = os.getenv("PIP_INDEX_URL", "https://pypi.org/simple")
    max_archive_bytes: int = int(os.getenv("MAX_ARCHIVE_BYTES", str(50 * 1024 * 1024)))
    max_extracted_bytes: int = int(os.getenv("MAX_EXTRACTED_BYTES", str(250 * 1024 * 1024)))
    max_archive_member_bytes: int = int(os.getenv("MAX_ARCHIVE_MEMBER_BYTES", str(100 * 1024 * 1024)))
    node_timeout_seconds: int = int(os.getenv("NODE_TIMEOUT_SECONDS", "180"))
    test_timeout_seconds: int = int(os.getenv("TEST_TIMEOUT_SECONDS", "900"))
    setup_repair_limit: int = max(0, min(5, int(os.getenv("SETUP_REPAIR_LIMIT", "2"))))


settings = Settings()
