"""Import every model so Base.metadata is complete (Alembic, tests)."""

from app.models.catalog import Category, Product, ProductFormVersion, ProductImage, Sku
from app.models.inventory import CardBatch, CardCode
from app.models.ops import (
    AIProvider,
    AuditLog,
    EmailLog,
    Job,
    Procurement,
    ReviewItem,
    Setting,
    Ticket,
    TicketMessage,
    VisitorDaily,
)
from app.models.orders import Delivery, Order, OrderEvent, OrderItem, Refund
from app.models.payments import (
    ChainCursor,
    ChainTransfer,
    CryptoAddress,
    PaymentAttempt,
    PaymentChannel,
    PaymentEvent,
)
from app.models.stores import Agent, Commission, Store, StoreDomain
from app.models.users import EmailLoginCode, User, UserIdentity, UserSession

__all__ = [
    "AIProvider",
    "Agent",
    "AuditLog",
    "CardBatch",
    "CardCode",
    "Category",
    "ChainCursor",
    "ChainTransfer",
    "Commission",
    "CryptoAddress",
    "Delivery",
    "EmailLog",
    "EmailLoginCode",
    "Job",
    "Order",
    "OrderEvent",
    "OrderItem",
    "PaymentAttempt",
    "PaymentChannel",
    "PaymentEvent",
    "Procurement",
    "Product",
    "ProductFormVersion",
    "ProductImage",
    "Refund",
    "ReviewItem",
    "Setting",
    "Sku",
    "Store",
    "StoreDomain",
    "Ticket",
    "TicketMessage",
    "User",
    "UserIdentity",
    "UserSession",
    "VisitorDaily",
]
