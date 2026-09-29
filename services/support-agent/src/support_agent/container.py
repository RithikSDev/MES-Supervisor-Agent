"""Object graph shared by the API and the worker."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mes_common.db import make_engine, make_session_factory
from support_agent.agent import Agent
from support_agent.clients import Api
from support_agent.knowledge import Knowledge
from support_agent.llm import ClaudeModel, Model
from support_agent.settings import Settings


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    api: Api
    agent: Agent

    async def aclose(self) -> None:
        await self.api.aclose()
        await self.engine.dispose()


def build_container(settings: Settings, *, api: Api | None = None, model: Model | None = None,
                    use_default_model: bool = True) -> Container:
    engine = make_engine(settings.database_url, pool_size=5)
    sf = make_session_factory(engine)
    api = api or Api(settings)
    if model is None and use_default_model and settings.anthropic_api_key:
        model = ClaudeModel(api_key=settings.anthropic_api_key, model=settings.agent_model,
                            max_tokens=settings.max_tokens, refusal_fallback=settings.refusal_fallback,
                            timeout_s=settings.anthropic_timeout_s)
    return Container(settings, engine, sf, api, Agent(settings, sf, api, model, Knowledge(settings.runbooks_dir)))
