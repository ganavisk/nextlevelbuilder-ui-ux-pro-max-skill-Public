"""Model registry: imports every ORM module so `Base.metadata` is complete.

SQLAlchemy resolves a string ForeignKey ("tenants.id") against the shared
metadata at flush time, not at class-definition time. A process that imports
only some model modules therefore gets a `NoReferencedTableError` the first
time it flushes a row whose FK target was never imported — a failure that
depends on import order rather than on anything the caller did wrong.

Every entry point (API, workers, scripts, tests) imports this module once so
that ordering stops mattering. Nothing here is re-exported for use; the
imports exist for their registration side effect alone.
"""

from app.modules.conversation import models as conversation_models
from app.modules.crm import models as crm_models
from app.modules.escalation import models as escalation_models
from app.modules.notifications import models as notifications_models
from app.modules.scheduling import models as scheduling_models
from app.modules.telephony import models as telephony_models
from app.modules.tenants import models as tenants_models

# Add a module here the moment it declares its first ORM model (analytics in
# Phase 7).
__all__ = [
    "conversation_models",
    "crm_models",
    "escalation_models",
    "notifications_models",
    "scheduling_models",
    "telephony_models",
    "tenants_models",
]
