from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.post import post_tags


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True, nullable=False)
    show_in_menu: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    menu_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    posts = relationship("Post", secondary=post_tags, back_populates="tags")
