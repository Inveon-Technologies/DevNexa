from sqlalchemy.orm import Session

from ..models import AuditLog


def audit(
    db: Session,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: int | str | None = None,
    message: str = "",
    **details,
) -> AuditLog:
    entry = AuditLog(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        message=message,
        details=details,
    )
    db.add(entry)
    db.flush()
    return entry
