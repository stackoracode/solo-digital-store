"""Admin backend. Every route requires a staff session with completed TOTP MFA (see deps.require_admin)."""

from fastapi import APIRouter, Depends

from app.web.deps import require_admin
from app.web.routes.admin import catalog, dashboard, ops, orders, payments, people, system

router = APIRouter(dependencies=[Depends(require_admin)])
for _module in (dashboard, catalog, orders, payments, ops, people, system):
    router.include_router(_module.router, prefix="/admin")
