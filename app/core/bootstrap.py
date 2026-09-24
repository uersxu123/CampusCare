import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import Base, engine
from app.core.security import hash_password
from app.models.entities import UserAccount


logger = logging.getLogger(__name__)


def create_schema() -> None:
    """Create a brand-new test schema only; deployed databases use Alembic."""
    Base.metadata.create_all(bind=engine)


def seed_data(db: Session) -> None:
    if db.query(UserAccount).count() == 0:
        admin = UserAccount(
            username="admin",
            display_name="Counselor Admin",
            password_hash=hash_password("admin123"),
        )
        admin.roles = {"ROLE_ADMIN", "ROLE_USER"}
        student = UserAccount(
            username="student",
            display_name="Demo Student",
            password_hash=hash_password("student123"),
        )
        student.roles = {"ROLE_USER"}
        db.add_all([admin, student])
        db.commit()

    settings = get_settings()
    from app.services.handbook_import import HandbookImporter

    HandbookImporter(db, settings).import_all()


def import_legacy_markdown_compat(service, importer, settings, root: Path) -> int:
    managed = importer.managed_raw_paths()
    files = [
        file
        for file in sorted((root / "knowledge").glob("*.md"))
        if file.relative_to(settings.project_root).as_posix() not in managed
    ]
    if not files:
        return 0
    if not settings.knowledge_legacy_markdown_bootstrap_enabled:
        logger.warning(
            "event=legacy_markdown_bootstrap_disabled unmanaged_count=%d",
            len(files),
            extra={"event": "legacy_markdown_bootstrap_disabled", "unmanaged_count": len(files)},
        )
        return 0
    logger.warning(
        "event=legacy_markdown_bootstrap_compat_enabled unmanaged_count=%d",
        len(files),
        extra={"event": "legacy_markdown_bootstrap_compat_enabled", "unmanaged_count": len(files)},
    )
    for file in files:
        relative = file.relative_to(settings.project_root).as_posix()
        if file.name == "risk-policy.md":
            domain = "SAFETY"
        elif file.name in {"academic-stress-and-burnout.md", "exam-season-guidance.md"}:
            domain = "ACADEMIC"
        else:
            domain = "MENTAL_HEALTH"
        service.ensure_source(file.name, file.read_text(encoding="utf-8"), domain)
    return len(files)
