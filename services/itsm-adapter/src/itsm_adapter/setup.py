"""Connection check for the real ServiceNow / Jira instances: `python -m itsm_adapter.setup [--create-groups]`.

Checks the connections configured in the admin UI (stored in the database), never prints secrets, and reports:
  - ServiceNow: the integration user can read incidents; the three tier assignment groups exist
    (--create-groups creates missing ones; that needs a user allowed to write sys_user_group, e.g. admin on a PDI)
  - Jira: the API token works; the project exists and has the configured Bug / Task issue types
Exit code 0 when everything needed is in place.
"""

import argparse
import asyncio
import sys

from itsm_adapter.connections import check_jira, check_servicenow
from itsm_adapter.container import build_container
from itsm_adapter.settings import Settings


async def check(s: Settings, create_groups: bool) -> bool:
    c = build_container(s)
    try:
        await c.registry.import_env_once()
        await c.registry.refresh(force=True)
        reg, ok = c.registry, True
        print(f"ServiceNow  {reg.sn.endpoint or '(not connected: Integrations -> ServiceNow / Jira)'}")
        if reg.error.get("servicenow"):
            print(f"  !! {reg.error['servicenow']}")
        if not reg.sn.configured:
            ok = False
        else:
            for x in await check_servicenow(reg.sn, reg.cfg.groups, create_groups=create_groups):  # type: ignore[arg-type]
                ok &= x["ok"] is not False
                print(f"  {'ok ' if x['ok'] else '-- ' if x['ok'] is None else '!! '} {x['name']}: {x['detail']}")
        print(f"Jira        {reg.jira.endpoint or '(not connected: L3 escalations stay in ServiceNow only)'}")
        if reg.jira.configured:
            types = [reg.cfg.jira_issue_type_bug, reg.cfg.jira_issue_type_task]
            for x in await check_jira(reg.jira, types):  # type: ignore[arg-type]
                ok &= x["ok"] is not False
                print(f"  {'ok ' if x['ok'] else '!! '} {x['name']}: {x['detail']}")
        print("READY" if ok else "NOT READY")
        return ok
    finally:
        await c.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--create-groups", action="store_true", help="create missing ServiceNow assignment groups")
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(check(Settings(), args.create_groups)) else 1)


if __name__ == "__main__":
    main()
