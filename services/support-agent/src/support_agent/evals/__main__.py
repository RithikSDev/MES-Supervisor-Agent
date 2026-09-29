"""Run the evaluation set against the real model: `python -m support_agent.evals [--only id,id] [--model m] [--no-save]`.

Every scenario is a real Claude conversation (it costs money: roughly $0.15-0.30 per scenario). Results are printed and
saved to the agent's database (shown on the AI support page, Evaluation tab)."""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal

from mes_common.db import make_engine, make_session_factory
from mes_common.ids import new_id
from support_agent.db import eval_run
from support_agent.evals.harness import run_scenario
from support_agent.evals.scenarios import SCENARIOS
from support_agent.knowledge import Knowledge
from support_agent.llm import ClaudeModel
from support_agent.settings import get_settings


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--model", help="model id (default: AGENT_MODEL)")
    ap.add_argument("--no-save", action="store_true", help="don't store the results")
    args = ap.parse_args()
    s = get_settings()
    if not s.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set")
        return 2
    model = ClaudeModel(api_key=s.anthropic_api_key, model=args.model or s.agent_model, max_tokens=s.max_tokens,
                        refusal_fallback=s.refusal_fallback, timeout_s=s.anthropic_timeout_s)
    chosen = [sc for sc in SCENARIOS if not args.only or sc.id in args.only.split(",")]
    knowledge = Knowledge(s.runbooks_dir)
    started = datetime.now(UTC)
    results = []
    for sc in chosen:
        r = await run_scenario(sc, model, s, knowledge)
        results.append(r)
        mark = "PASS" if r.passed else "FAIL"
        print(f"{mark}  {r.id:22} {r.tier}  ${r.cost_usd:.3f}  {r.seconds:5.1f}s  actions={r.actions}")
        for check, ok, detail in r.checks:
            if not ok:
                print(f"        x {check}: {detail}")
    passed = sum(r.passed for r in results)
    cost = sum(r.cost_usd for r in results)
    print(f"\n{passed}/{len(results)} passed · ${cost:.2f} · model {model.name}")
    if not args.no_save:
        engine = make_engine(s.database_url, pool_size=1)
        sf = make_session_factory(engine)
        async with sf() as db, db.begin():
            await db.execute(eval_run.insert().values(
                id=new_id(), model=model.name, started_at=started, finished_at=datetime.now(UTC), total=len(results),
                passed=passed, cost_usd=Decimal(str(round(cost, 6))), results=[r.as_dict() for r in results]))
        await engine.dispose()
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
