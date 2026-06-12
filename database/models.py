"""Модели данных (SQLAlchemy 2.0) с мультитенантностью по telegram_id.

Архитектура построена на абстрактных миксинах, чтобы не дублировать общие
поля в каждой таблице:

    Base                — декларативная база
    ├─ TenantMixin      — telegram_id (жёсткая изоляция данных по юзеру)
    ├─ PrimaryKeyMixin  — суррогатный PK + бизнес-идентификатор Uzum (uzum_id)
    ├─ TimestampMixin   — служебные метки синхронизации (synced_at / updated_at)
    └─ RawPayloadMixin  — «сырой» JSON ответа для отказоустойчивости

    MarketplaceEntity = Base + миксины — общий предок доменных таблиц.
    Уникальность бизнес-сущности — composite (telegram_id, uzum_id), а не
    глобальный uzum_id, иначе данные разных магазинов конфликтуют.

Доменные таблицы: Order / OrderItem / Invoice / Barcode / Return / ReturnItem /
SkuBarcode. Плюс служебная таблица UserShop (подключённые магазины бота).
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    declared_attr,
    mapped_column,
    relationship,
)

# raw_payload: на PostgreSQL — JSONB (бинарный, сжатие + быстрые GIN-индексы по
# содержимому), на прочих диалектах (SQLite-дев) — обычный JSON. with_variant
# даёт нужный тип под каждый диалект из одного объявления.
_JSON_PAYLOAD = JSON().with_variant(JSONB(), "postgresql")

from utils.crypto import EncryptedToken


def tenant_unique(table: str) -> UniqueConstraint:
    """Composite-уникальность (telegram_id, uzum_id) для доменной таблицы."""
    return UniqueConstraint("telegram_id", "uzum_id", name=f"uq_{table}_tenant_uzum")


# --------------------------------------------------------------------------- #
#  База и абстрактные миксины
# --------------------------------------------------------------------------- #
class Base(DeclarativeBase):
    """Единая декларативная база для всех моделей."""

    type_annotation_map = {dict[str, Any]: JSON}


class TenantMixin:
    """Идентификатор владельца данных в Telegram — жёсткая изоляция по юзеру."""

    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)


class PrimaryKeyMixin:
    """Суррогатный автоинкрементный PK + бизнес-идентификатор из Uzum.

    `uzum_id` — реальный id сущности на стороне маркетплейса. Уникальность —
    НЕ глобальная, а в паре с telegram_id (см. tenant_unique в __table_args__),
    upsert строится по (telegram_id, uzum_id).
    """

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    uzum_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)


class TimestampMixin:
    """Служебные метки времени синхронизации (не бизнес-даты Uzum)."""

    synced_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class RawPayloadMixin:
    """Сырой ответ API — на случай новых полей, которых ещё нет в модели.

    Тип — JSONB на PostgreSQL (см. _JSON_PAYLOAD): бинарное хранение со сжатием
    больших JSON-ответов Uzum и возможностью индексировать содержимое (GIN).
    """

    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(_JSON_PAYLOAD, nullable=True)


class MarketplaceEntity(Base, TenantMixin, PrimaryKeyMixin, TimestampMixin, RawPayloadMixin):
    """Абстрактный предок доменных сущностей маркетплейса (мультитенантный)."""

    __abstract__ = True

    @declared_attr.directive
    def __tablename__(cls) -> str:  # noqa: N805
        # CamelCase -> snake_case + 's'   (Order -> orders, OrderItem -> order_items)
        name = cls.__name__
        snake = "".join(
            f"_{c.lower()}" if c.isupper() and i else c.lower()
            for i, c in enumerate(name)
        )
        return f"{snake}s"


# --------------------------------------------------------------------------- #
#  Заказы (FBS/DBS)
# --------------------------------------------------------------------------- #
class Order(MarketplaceEntity):
    """FBS/DBS заказ продавца (источник: /v2/fbs/orders, /v1/fbs/order/{id})."""

    shop_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    status: Mapped[str | None] = mapped_column(String(48), index=True)
    scheme: Mapped[str | None] = mapped_column(String(8))  # FBS | DBS | FBO
    price: Mapped[int | None] = mapped_column(BigInteger)
    invoice_number: Mapped[int | None] = mapped_column(BigInteger, index=True)

    # Бизнес-даты Uzum (приходят строками ISO / unix-ms — нормализуются в маппере)
    date_created: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    accept_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    deliver_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Даты фактических событий в ПВЗ — точка отсчёта 7-дневного SLA на возврат.
    return_date: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    date_cancelled: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    __table_args__ = (
        tenant_unique("orders"),
        Index("ix_orders_tenant_status", "telegram_id", "status"),
    )


class OrderItem(MarketplaceEntity):
    """Позиция заказа (источник: SellerOrderItemDto)."""

    order_pk: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    order_uzum_id: Mapped[int] = mapped_column(BigInteger, index=True)

    sku_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # index: ключ JOIN аналитики (order_items.sku_title = user_products.article).
    sku_title: Mapped[str | None] = mapped_column(String(512), index=True)
    product_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    product_title: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str | None] = mapped_column(String(32))
    amount: Mapped[int | None] = mapped_column()
    seller_price: Mapped[int | None] = mapped_column(BigInteger)
    # Проставляется на 2-м этапе из накладных по совпадению order_uzum_id + sku_id.
    barcode: Mapped[str | None] = mapped_column(String(64), index=True)

    order: Mapped["Order"] = relationship(back_populates="items")

    __table_args__ = (tenant_unique("order_items"),)


# --------------------------------------------------------------------------- #
#  Накладные FBS + штрихкоды
# --------------------------------------------------------------------------- #
class Invoice(MarketplaceEntity):
    """FBS-накладная (источник: /v1/fbs/invoice, FbsInvoiceDto)."""

    number: Mapped[int | None] = mapped_column(BigInteger, index=True)
    status: Mapped[str | None] = mapped_column(String(48), index=True)
    full_price: Mapped[int | None] = mapped_column(BigInteger)
    accepted_price: Mapped[int | None] = mapped_column(BigInteger)
    number_orders: Mapped[int | None] = mapped_column()
    number_accepted_orders: Mapped[int | None] = mapped_column()
    stock_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    stock_title: Mapped[str | None] = mapped_column(String(256))
    ettn_id: Mapped[str | None] = mapped_column(String(128))

    date_created: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    barcodes: Mapped[list["Barcode"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )

    __table_args__ = (tenant_unique("invoices"),)


class Barcode(MarketplaceEntity):
    """Штрихкод SKU в составе накладной.

    Источник: FbsInvoiceOrderItemDto (/v1/fbs/invoice/{id}/orders) — именно тут
    лежит поле `barcode` для физической приёмки/печати этикеток.
    """

    invoice_pk: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), index=True
    )
    order_uzum_id: Mapped[int] = mapped_column(BigInteger, index=True)

    barcode: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    sku_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    sku_title: Mapped[str | None] = mapped_column(String(512))
    title: Mapped[str | None] = mapped_column(String(512))
    amount: Mapped[int | None] = mapped_column()
    price: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str | None] = mapped_column(String(32))

    invoice: Mapped["Invoice"] = relationship(back_populates="barcodes")

    __table_args__ = (
        tenant_unique("barcodes"),
        Index("ix_barcodes_order_barcode", "order_uzum_id", "barcode", unique=False),
    )


# --------------------------------------------------------------------------- #
#  Возвраты
# --------------------------------------------------------------------------- #
class Return(MarketplaceEntity):
    """Возврат продавца (источник: /v1/return, SellerReturnDto)."""

    shop_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    status: Mapped[str | None] = mapped_column(String(48), index=True)
    type: Mapped[str | None] = mapped_column(String(16))  # DEFECTED | RETURN | FBS
    external_number: Mapped[str | None] = mapped_column(String(128), index=True)
    total_amount: Mapped[int | None] = mapped_column()

    date_created: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_date: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["ReturnItem"]] = relationship(
        back_populates="return_", cascade="all, delete-orphan"
    )

    __table_args__ = (tenant_unique("returns"),)


class ReturnItem(MarketplaceEntity):
    """Позиция возврата (источник: SellerReturnItemDto)."""

    return_pk: Mapped[int] = mapped_column(
        ForeignKey("returns.id", ondelete="CASCADE"), index=True
    )
    return_uzum_id: Mapped[int] = mapped_column(BigInteger, index=True)

    sku_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    sku_title: Mapped[str | None] = mapped_column(String(512))
    product_title: Mapped[str | None] = mapped_column(String(512))
    amount: Mapped[int | None] = mapped_column()
    packed_amount: Mapped[int | None] = mapped_column()
    purchase_price: Mapped[int | None] = mapped_column(BigInteger)

    return_: Mapped["Return"] = relationship(back_populates="items")

    __table_args__ = (tenant_unique("return_items"),)


# --------------------------------------------------------------------------- #
#  Каталог SKU → штрихкод
# --------------------------------------------------------------------------- #
class SkuBarcode(MarketplaceEntity):
    """SKU из каталога магазина с штрихкодом.

    Источник: /v1/product/shop/{shopId} → SellerProductCard.skuList[] (SkuForTable).
    Полный справочник {SKU → barcode} для 100% покрытия отчёта по невозвратам.
    uzum_id = skuId.
    """

    shop_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    sku_title: Mapped[str | None] = mapped_column(String(512), index=True)
    sku_full_title: Mapped[str | None] = mapped_column(String(512))
    product_title: Mapped[str | None] = mapped_column(String(512))
    article: Mapped[str | None] = mapped_column(String(128), index=True)
    seller_item_code: Mapped[str | None] = mapped_column(String(128), index=True)
    barcode: Mapped[str | None] = mapped_column(String(64), index=True)

    __table_args__ = (tenant_unique("sku_barcodes"),)


# --------------------------------------------------------------------------- #
#  Пользователи бота (мультиаккаунтность)
# --------------------------------------------------------------------------- #
class UserRole(str, enum.Enum):
    """Иерархия ролей (RBAC). В БД persist'ится ИМЯ члена (USER/MANAGER/…),
    значение (lowercase) удобно для команд/сравнений (str-enum)."""

    USER = "user"
    MANAGER = "manager"
    ADMIN = "admin"
    ROOT = "root"


class User(Base):
    """Пользователь бота и его тариф (Free / Premium).

    Отдельная сущность от UserShop: один telegram_id = один User, но может иметь
    несколько магазинов. Premium активен, если subscription_tier == 'premium' И
    subscription_expires_at в будущем (см. repository.is_user_premium).
    """

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subscription_tier: Mapped[str] = mapped_column(
        String(16), nullable=False, default="free", server_default="free"
    )
    # Витринное название плана (Бесплатный / Premium / Pro / Ultra). tier — логика
    # доступа, plan_name — человекочитаемая подпись в «Моём кабинете».
    plan_name: Mapped[str] = mapped_column(
        String(32), nullable=False, default="Бесплатный", server_default="Бесплатный"
    )
    # subscription_expires_at = «конец подписки» (subscription_ends_at из ТЗ): ставит
    # биллинг (activate_premium). Отдельное поле-дубль не заводим — один источник истины.
    subscription_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Профиль Telegram — для красивого вывода менеджеров (имя/юзернейм вместо ID).
    # Заполняется при /start и приёме инвайта (где есть message.from_user).
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    # Enterprise-контроль доступа (RBAC).
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), nullable=False, default=UserRole.USER, server_default="USER"
    )
    is_banned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ShopManager(Base):
    """Доступ менеджера к магазину владельца (Один магазин — много менеджеров).

    Менеджер (manager_telegram_id) получает доступ к данным/аналитике владельца
    (owner_telegram_id) по инвайт-ссылке. shop_id — uzum_shop_id владельца на момент
    выдачи доступа (опционально, для будущей привязки к конкретному магазину).
    """

    __tablename__ = "shop_managers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    shop_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    owner_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    manager_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "owner_telegram_id", "manager_telegram_id", name="uq_shop_manager"
        ),
    )


class SupportTicket(Base):
    """Связка «пользователь ↔ топик техподдержки» в супергруппе (Forum).

    Один пользователь = один топик (тема) в группе поддержки. Прямой поиск —
    по telegram_id (создать/переслать в топик), обратный — по topic_id (ответ
    админа из топика → в личку юзеру), поэтому topic_id под индексом.
    """

    __tablename__ = "support_tickets"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    topic_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaymentLog(Base):
    """Аудит платежей за Premium — для разбора зависших оплат и ручного начисления.

    Запись создаётся при выставлении инвойса (status='created') и закрывается при
    SUCCESSFUL_PAYMENT (status='completed'). Если автоплатёж Click завис, в логе
    останется 'created' — админ это видит в /admin_payments и начисляет вручную.
    """

    __tablename__ = "payment_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    payload: Mapped[str] = mapped_column(String(64))
    amount: Mapped[int] = mapped_column(Integer)        # сумма в СУМАХ (не в тийинах)
    # Идемпотентность начислений: charge_id Telegram уникален на платёж — повторный
    # SUCCESSFUL_PAYMENT (передоставка апдейта) упрётся в unique и не начислит дни дважды.
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="created", server_default="created"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class SystemSettings(Base):
    """Глобальные настройки системы (key-value). Напр. maintenance_mode='true'."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserShop(Base, TimestampMixin):
    """Подключённый магазин пользователя бота (мультимагазинность).

    У одного telegram_id может быть несколько подключённых магазинов (по одному
    токену доступно несколько shopId). Активен ровно один (is_active=True) — его
    данные лежат в доменных таблицах (изоляция по telegram_id, см. решение
    «purge + ре-синк при смене магазина»). Токен шифруется (Fernet).
    """

    __tablename__ = "user_shops"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    uzum_shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    shop_name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255))
    # Прозрачное шифрование Fernet: в Python — чистый токен, в БД — шифртекст.
    uzum_token: Mapped[str] = mapped_column(EncryptedToken(2048), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Время последней успешной синхронизации этого магазина (кэш отчёта 30 мин).
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("telegram_id", "uzum_shop_id", name="uq_user_shops_tg_shop"),
    )


class FinanceSnapshot(Base, TimestampMixin):
    """Снимок финансов активного магазина (баланс/выплаты), один на пользователя.

    Чистится при смене магазина (purge_user_data). Поля сумм — в сум.
    """

    __tablename__ = "finance_snapshots"

    # telegram_id — PK (индексирован), shop_id — отдельный индекс для быстрого чтения.
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    available: Mapped[int] = mapped_column(BigInteger, default=0)   # 💵 к выводу
    pending: Mapped[int] = mapped_column(BigInteger, default=0)     # ⏳ в ожидании
    commissions: Mapped[int] = mapped_column(BigInteger, default=0)  # 📉 удержания
    payments: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)  # 💳 последние выплаты
    has_data: Mapped[bool] = mapped_column(Boolean, default=False)
    # Время последнего УСПЕШНОГО обновления финансов (свой кэш, 30 мин).
    finance_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class UserProduct(MarketplaceEntity):
    """Товар (SKU) магазина пользователя из Uzum. uzum_id = skuId.

    Привязан к telegram_id (изоляция) и shop_id (магазин). Чистится при смене
    магазина (purge_user_data). purchase_price изначально NULL — задаётся юзером.
    """

    shop_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    product_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    title: Mapped[str | None] = mapped_column(String(512))
    sku_title: Mapped[str | None] = mapped_column(String(512))
    current_price: Mapped[int | None] = mapped_column(BigInteger)   # цена продажи на маркетплейсе
    fbo_stock: Mapped[int | None] = mapped_column()                  # остаток на складе Uzum
    fbs_stock: Mapped[int | None] = mapped_column()                  # остаток на своём складе
    category_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # → uzum_categories.id
    purchase_price: Mapped[int | None] = mapped_column(BigInteger)   # закупка (NULL пока не задана)
    # index: ключ JOIN аналитики (user_products.article = order_items.sku_title).
    article: Mapped[str | None] = mapped_column(String(128), index=True)  # артикул SKU («ТЕМНБОР-L»)
    sku_root: Mapped[str | None] = mapped_column(String(128), index=True)  # корень для группировки
    # Базовый URL превью из Uzum (поле previewImage, без суффикса размера).
    # Полный URL картинки строится при рендере (+ /t_product_540_high.jpg).
    image_url: Mapped[str | None] = mapped_column(String(512))
    # Штрихкод SKU из каталога — обязателен для POST-обновления остатка FBS
    # (RestSellerSkuFbsAmountDto.barcode). См. services.products.update_fbs_stock_remote.
    barcode: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (tenant_unique("user_products"),)


class UzumCategory(Base):
    """Справочник комиссий Uzum по категориям (глобальный, не привязан к юзеру).

    Заполняется из Excel «Новые комиссии c калькулятором.xlsx» скриптом
    scripts/parse_commissions.py. Используется калькулятором юнит-экономики.
    Комиссии хранятся долями (0.1 = 10%).
    """

    __tablename__ = "uzum_categories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # category ID из Excel
    display_name: Mapped[str] = mapped_column(String(512))         # «Одежда -> Женская -> Платья»
    search_text: Mapped[str] = mapped_column(String(512), index=True)  # для поиска LIKE (lower)
    comm_fbo: Mapped[float] = mapped_column(Float, default=0.0)
    comm_fbs: Mapped[float] = mapped_column(Float, default=0.0)
    is_kgt: Mapped[bool] = mapped_column(Boolean, default=False)   # крупногабарит → логистика 20000


# --------------------------------------------------------------------------- #
#  FBS-логистика: контроль дедлайнов отгрузки + акты приёма-передачи
# --------------------------------------------------------------------------- #
class FBSOrder(Base):
    """FBS-заказ под контролем дедлайна отгрузки (модуль FBS-логистики).

    Отдельно от orders: здесь лёгкий контур «заказ → таймер штрафа» (id заказа,
    SKU, момент появления в ЛК), без полного снимка заказа. Регламент Uzum —
    24 часа на сборку/передачу (см. utils.fbs_calc.calculate_fbs_deadline).
    """

    __tablename__ = "fbs_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    # ID заказа в системе Uzum; уникален в паре с telegram_id (мультитенантность).
    uzum_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sku_title: Mapped[str] = mapped_column(String(256), nullable=False)
    # Время появления заказа в ЛК — точка отсчёта 24-часового дедлайна.
    order_created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="NEW", server_default="NEW"
    )

    __table_args__ = (
        UniqueConstraint("telegram_id", "uzum_order_id", name="uq_fbs_orders_tenant_order"),
    )


class ShippingAct(Base):
    """Акт приёма-передачи FBS-отгрузки (документ из ЛК Uzum)."""

    __tablename__ = "shipping_acts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    act_number: Mapped[str] = mapped_column(String(64), nullable=False)
    total_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    pdf_url: Mapped[str | None] = mapped_column(String(512), nullable=True)


__all__ = [
    "Base",
    "MarketplaceEntity",
    "TenantMixin",
    "PrimaryKeyMixin",
    "TimestampMixin",
    "RawPayloadMixin",
    "tenant_unique",
    "User",
    "UserRole",
    "ShopManager",
    "SupportTicket",
    "PaymentLog",
    "FBSOrder",
    "ShippingAct",
    "SystemSettings",
    "UserShop",
    "FinanceSnapshot",
    "UzumCategory",
    "UserProduct",
    "Order",
    "OrderItem",
    "Invoice",
    "Barcode",
    "Return",
    "ReturnItem",
    "SkuBarcode",
]
