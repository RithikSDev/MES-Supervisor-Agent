import { useMe } from "../auth";
import { ModuleCardGrid, useModuleCounts } from "../components/ModuleGrid";
import { PageHeader } from "../components/Layout";
import { GROUP_DESC, type ModuleGroup, visibleModules } from "../modules";

/** One module group as its own page — Home links here instead of listing every module directly. */
export function CategoryPage({ group }: { group: ModuleGroup }) {
  const me = useMe();
  const modules = visibleModules(me.permissions);
  const items = modules.filter((m) => m.group === group);
  const { counts, alertsEscalated } = useModuleCounts(group === "Support");
  return (
    <>
      <PageHeader title={group} sub={GROUP_DESC[group]} />
      <ModuleCardGrid items={items} allModules={modules} counts={counts} alertsEscalated={alertsEscalated} />
    </>
  );
}
