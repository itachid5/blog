from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.setting import Setting


def get_settings_map(db: Session) -> dict[str, str]:
    return {setting.key: setting.value for setting in db.scalars(select(Setting)).all()}


def upsert_setting(db: Session, key: str, value: str) -> None:
    setting = db.scalar(select(Setting).where(Setting.key == key))
    if setting is None:
        db.add(Setting(key=key, value=value))
    else:
        setting.value = value
