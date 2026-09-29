"""Role -> permission mapping. Endpoints check permissions, never role names.

Roles come from Keycloak (realm roles). *Where* a user may act (plant/line) comes from MES user_scope rows.
"""

from enum import StrEnum


class Perm(StrEnum):
    ORG_READ = "org:read"
    MASTERDATA_READ = "masterdata:read"
    WORK_ORDER_READ = "work_order:read"
    WORK_ORDER_MANAGE = "work_order:manage"  # dispatch, hold, resume, priority
    SCHEDULE_READ = "schedule:read"
    SCHEDULE_EDIT = "schedule:edit"
    SCHEDULE_PUBLISH = "schedule:publish"
    CONFIRMATION_CREATE = "confirmation:create"
    CONFIRMATION_READ = "confirmation:read"
    AUDIT_READ = "audit:read"
    AUDIT_VERIFY = "audit:verify"
    EXECUTION_OPERATE = "execution:operate"  # start/pause/report/complete operations
    EXECUTION_READ = "execution:read"
    MATERIAL_ISSUE = "material:issue"  # issue components to an order
    INVENTORY_READ = "inventory:read"
    INVENTORY_MANAGE = "inventory:manage"  # receipts, transfers
    TERMINAL_ADMIN = "terminal:admin"  # register/enrol/revoke terminals
    OPERATOR_ADMIN = "operator:admin"  # create operators, set PINs
    SYSTEM_RESET = "system:reset"  # empty all data and re-create the demo plant (demo installations only)
    QUALITY_READ = "quality:read"
    QUALITY_INSPECT = "quality:inspect"  # record inspection results
    QUALITY_DECIDE = "quality:decide"  # usage decisions, NC dispositions, quarantine (e-signed)
    QUALITY_PLAN = "quality:plan"  # maintain inspection plans
    NC_CREATE = "nc:create"  # report a defect / nonconformance
    TRACE_READ = "trace:read"  # genealogy
    MAINT_READ = "maintenance:read"
    MAINT_REPORT = "maintenance:report"  # report a breakdown / request, machine back up
    MAINT_EXECUTE = "maintenance:execute"  # create/start/complete maintenance orders, spare parts
    MAINT_PLAN = "maintenance:plan"  # preventive plans
    TICKET_READ = "ticket:read"
    TICKET_CREATE = "ticket:create"  # raise and comment
    TICKET_MANAGE = "ticket:manage"  # acknowledge, severity, escalate, resolve
    SUPPORT_TEAM = "support:team"  # maintain the support engineers tickets are assigned to


_READ = {
    Perm.ORG_READ,
    Perm.MASTERDATA_READ,
    Perm.WORK_ORDER_READ,
    Perm.SCHEDULE_READ,
    Perm.CONFIRMATION_READ,
    Perm.EXECUTION_READ,
    Perm.INVENTORY_READ,
    Perm.QUALITY_READ,
    Perm.TRACE_READ,
    Perm.MAINT_READ,
    Perm.TICKET_READ,
}
_RAISE = {Perm.TICKET_CREATE, Perm.MAINT_REPORT}

ROLE_PERMISSIONS: dict[str, frozenset[Perm]] = {
    "operator": frozenset(
        _READ | _RAISE
        | {Perm.CONFIRMATION_CREATE, Perm.EXECUTION_OPERATE, Perm.MATERIAL_ISSUE, Perm.QUALITY_INSPECT, Perm.NC_CREATE}
    ),
    "supervisor": frozenset(
        _READ
        | {
            Perm.WORK_ORDER_MANAGE,
            Perm.SCHEDULE_EDIT,
            Perm.SCHEDULE_PUBLISH,
            Perm.CONFIRMATION_CREATE,
            Perm.AUDIT_READ,
            Perm.EXECUTION_OPERATE,
            Perm.MATERIAL_ISSUE,
            Perm.INVENTORY_MANAGE,
            Perm.TERMINAL_ADMIN,
            Perm.OPERATOR_ADMIN,
            Perm.QUALITY_INSPECT,
            Perm.NC_CREATE,
            Perm.MAINT_EXECUTE,
            Perm.TICKET_MANAGE,
            Perm.SUPPORT_TEAM,
        }
        | _RAISE
    ),
    "quality_engineer": frozenset(
        _READ | _RAISE | {Perm.AUDIT_READ, Perm.QUALITY_INSPECT, Perm.QUALITY_DECIDE, Perm.QUALITY_PLAN, Perm.NC_CREATE}
    ),
    "maintenance_tech": frozenset(_READ | _RAISE | {Perm.MAINT_EXECUTE, Perm.MAINT_PLAN}),
    # service account of the MES MCP server (AI agents): read everything it may show, raise and triage tickets
    "ai_agent": frozenset(_READ | {Perm.TICKET_CREATE, Perm.TICKET_MANAGE}),
    # service account of the AI support agent (support-agent service): triage and work tickets, read the audit trail,
    # and carry out the support actions its policy allows (end a stuck terminal session, report a machine back up).
    # What it may do on its own vs. only propose is decided per action by the agent's policy, not by these rights.
    "ai_support_agent": frozenset(_READ | _RAISE | {Perm.TICKET_MANAGE, Perm.AUDIT_READ, Perm.TERMINAL_ADMIN}),
    "admin": frozenset(Perm),
}

ALL_PLANTS_ROLES = {"admin"}

# A PIN session on a shop-floor terminal always acts with operator rights, whoever logs in (least privilege:
# supervisors do planning in the admin UI, not on a shared kiosk).
TERMINAL_ROLES = frozenset({"operator"})
# ...plus maintenance technician rights when the PIN belongs to a technician (technician mode on the terminal)
TERMINAL_EXTRA_ROLES = frozenset({"maintenance_tech"})


def permissions_for(roles: frozenset[str] | set[str]) -> frozenset[Perm]:
    out: set[Perm] = set()
    for r in roles:
        out |= ROLE_PERMISSIONS.get(r, frozenset())
    return frozenset(out)
