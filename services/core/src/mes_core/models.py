"""Imports every module's ORM models so Base.metadata is complete (Alembic, tests)."""

from mes_core.modules.compliance import models as compliance_models  # noqa: F401
from mes_core.modules.execution import models as execution_models  # noqa: F401
from mes_core.modules.identity import models as identity_models  # noqa: F401
from mes_core.modules.inventory import models as inventory_models  # noqa: F401
from mes_core.modules.maintenance import models as maintenance_models  # noqa: F401
from mes_core.modules.masterdata import models as masterdata_models  # noqa: F401
from mes_core.modules.org import models as org_models  # noqa: F401
from mes_core.modules.planning import models as planning_models  # noqa: F401
from mes_core.modules.quality import models as quality_models  # noqa: F401
from mes_core.modules.ticketing import models as ticketing_models  # noqa: F401
from mes_core.shared.db.base import Base
from mes_core.shared.messaging import messaging  # noqa: F401  (registers messaging.* tables)

metadata = Base.metadata
