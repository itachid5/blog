from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.category import Category
from app.models.post import Post
from app.utils.slug import slugify


def create_category(db: Session, name: str, description: str = "", show_in_menu: bool = False, menu_order: int = 0) -> Category:
    category = Category(
        name=name.strip(),
        slug=unique_category_slug(db, name),
        description=description.strip(),
        show_in_menu=show_in_menu,
        menu_order=menu_order,
    )
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


def update_category(db: Session, category: Category, name: str, description: str = "", show_in_menu: bool = False, menu_order: int = 0) -> Category:
    clean_name = name.strip()
    if clean_name and clean_name != category.name:
        category.name = clean_name
        category.slug = unique_category_slug(db, clean_name, category.id)
    category.description = description.strip()
    category.show_in_menu = show_in_menu
    category.menu_order = menu_order
    db.commit()
    db.refresh(category)
    return category


def menu_categories(db: Session) -> list[Category]:
    return db.scalars(select(Category).where(Category.show_in_menu.is_(True)).order_by(Category.menu_order, Category.name)).all()


def categories_with_published_counts(db: Session) -> list[tuple[Category, int]]:
    return db.execute(
        select(Category, func.count(Post.id))
        .outerjoin(Post, (Post.category_id == Category.id) & (Post.status == "published"))
        .group_by(Category.id)
        .order_by(Category.name)
    ).all()


def unique_category_slug(db: Session, value: str, category_id: int | None = None) -> str:
    base = slugify(value)
    slug = base
    count = 2
    while True:
        query = select(Category).where(Category.slug == slug)
        if category_id is not None:
            query = query.where(Category.id != category_id)
        if db.scalar(query) is None:
            return slug
        slug = f"{base}-{count}"
        count += 1
