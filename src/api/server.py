"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for MFG-C2-095 AquacultureEarlyWarningGraph.
# Entry points are adapters only — no business logic here.
#
# wire_services() constructs the real LLM client, incident store, and
# weather client and injects them via config — MUST be called before the
# first /invoke or the graph falls back to deterministic/no-op behavior
# for every mode; unwired services can otherwise fail on the first invocation.

import os
import secrets
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.secrets.base import MissingSecret
from shared.secrets import factory as secrets_factory
from src.graph.graph import AquacultureEarlyWarningGraph

NAMESPACE = "mfg"
AGENT_NAME = "mfg-c2-095"


def wire_services() -> dict:
    """Construct real service clients for production use.

    Returns a config dict suitable for AquacultureEarlyWarningGraph(config=...).
    Import of the Anthropic client is local so tests that never call
    wire_services() do not need the `[anthropic]` extra installed.
    """
    from src.services.incident_store import InMemoryIncidentStore  # TODO: replace in production deployments
    from src.services.weather_client import JmaWeatherClient

    # The concrete model identity is a deployment-time decision, not hardcoded
    # here. Exactly two conditions degrade to llm=None; the service must keep
    # serving the deterministic path, so neither may stop startup:
    #   (a) the optional `[anthropic]` extra is not installed
    #   (b) ANTHROPIC_API_KEY is not provisioned in this environment
    # Anything else — a renamed/absent public symbol, a wrong constructor
    # signature, a broken transitive import — is a WIRING BUG and propagates.
    #
    # 🔴 This block previously imported `AnthropicLLMClient`, a name that does
    # not exist in `shared.services.llm.anthropic_client` (the class is
    # `AnthropicClient`), and constructed it with no arguments although
    # `AnthropicClient.__init__` requires `config["api_key"]`. A blanket
    # `except Exception` swallowed both, so production silently ran with
    # `llm=None` on every request while `config/agent.yaml` declared
    # `generation_mode: "llm"` — and `wire_services()` is never exercised by CI.
    llm = None
    try:
        from shared.services.llm.anthropic_client import AnthropicClient
    except ModuleNotFoundError as exc:
        # (a) Only the optional dependency itself. A ModuleNotFoundError naming
        # anything else means our own import graph is broken — do not swallow.
        # Note `from X import Name` raises plain ImportError when the MODULE
        # exists but the NAME does not, so that case propagates here: it is
        # precisely the `AnthropicLLMClient` typo this block used to hide.
        if exc.name != "langchain_anthropic":
            raise
    else:
        provider = secrets_factory(namespace=NAMESPACE, agent_name=AGENT_NAME)
        try:
            # Narrow on purpose: ONLY the secret lookup may degrade. Wrapping
            # the constructor too would swallow a future wiring defect that
            # happens to raise MissingSecret from inside the client.
            api_key = provider.require("ANTHROPIC_API_KEY")
        except MissingSecret:
            # (b) Key not provisioned — documented degradation. Raising here
            # would abort module import and take /health down with it.
            api_key = None
        if api_key is not None:
            llm = AnthropicClient({"api_key": api_key})

    return {
        "llm": llm,
        "incident_store": InMemoryIncidentStore(),  # TODO: replace with a durable store in production
        "weather_client": JmaWeatherClient(),
        "max_retry": 3,
    }


app = FastAPI(title="AquacultureEarlyWarningAgent")

agent = AquacultureEarlyWarningGraph(config=wire_services())
agent.compile()
agent.provision_secrets(secrets_factory(namespace=NAMESPACE, agent_name=AGENT_NAME))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Optional standalone caller authentication: when
    # INVOKE_AUTH_TOKEN is set on the server environment, callers that no upstream
    # middleware vouched for (still ANONYMOUS) must present it as a Bearer token
    # and run at VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary (standalone equivalent of
    # platform AuthMiddleware) — a deployment-level caller credential, not an
    # agent secret, so ctx.secrets does not apply because no InvocationContext
    # exists before authentication.
    #
    # Without this block every HTTP caller stays ANONYMOUS, so the S-1 trust gate
    # refuses the privileged nodes and /invoke can never succeed.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx)


@app.get("/health")
def health():
    return {"status": "ok", "agent": AGENT_NAME}
