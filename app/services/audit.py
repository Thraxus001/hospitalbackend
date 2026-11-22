from sqlalchemy.orm import Session

from ..models import AuditLog, User


def audit(db: Session, actor: User, action: str, detail: str = ""):
    db.add(AuditLog(admin_id=actor.id, action=action, detail=detail[:300]))
