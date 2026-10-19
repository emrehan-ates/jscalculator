"""Environment-driven configuration for the eval harness.

Every value comes from the same .env the Chainlit app already uses, so a run is
reproducible from configuration alone. Nothing about a specific fabric is
hard-coded here.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# LangSmith is not used anywhere in this project; force it off regardless of
# what a .env sets, so it can never intercept an LLM call or its network
# timeouts. Phoenix/OpenInference is the only tracing path.
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ.pop("LANGCHAIN_API_KEY", None)
os.environ.pop("LANGCHAIN_ENDPOINT", None)
os.environ.pop("LANGCHAIN_PROJECT", None)

# ---------------------------------------------------------------- Phoenix ----
# Same collector the app writes to; a SEPARATE project so eval traces never mix
# with production conversation traces.
PHOENIX_ENDPOINT = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://10.6.107.105:6006")
APP_PROJECT_NAME = os.environ.get("PHOENIX_PROJECT_NAME", "aci-orchestrator")
EVAL_PROJECT_NAME = os.environ.get("EVAL_PHOENIX_PROJECT_NAME", "aci-orchestrator-evals")
DEFAULT_DATASET_NAME = os.environ.get("EVAL_DATASET_NAME", "aci-agent-regression")

# PHOENIX_ENDPOINT is the OTLP trace-export URL (often "http://host:6006/v1/traces");
# the REST client (datasets/experiments) needs the bare host:port instead, or the
# two get concatenated into ".../v1/traces/v1/datasets/upload" (405). Allow an
# explicit override, otherwise strip the OTLP suffix.
_OTLP_SUFFIXES = ("/v1/traces",)


def _rest_base_url(endpoint: str) -> str:
    stripped = endpoint.rstrip("/")
    for suffix in _OTLP_SUFFIXES:
        if stripped.endswith(suffix):
            return stripped[: -len(suffix)]
    return stripped


PHOENIX_REST_BASE_URL = os.environ.get("PHOENIX_BASE_URL") or _rest_base_url(PHOENIX_ENDPOINT)

# ------------------------------------------------------------ Run behaviour --
# One example at a time by default. supervisor._load_tools publishes the MCP
# tool registry into a task-local ContextVar and each fabric owns exactly ONE
# stdio MCP session, so parallel examples would contend for the same session.
CONCURRENCY = int(os.environ.get("EVAL_CONCURRENCY", "1"))
# Per-example wall clock budget. An example that blows it is recorded as a
# scored failure, not skipped.
TASK_TIMEOUT_S = float(os.environ.get("EVAL_TASK_TIMEOUT", "300"))

# ------------------------------------------------------------------- Gate ----
# Evaluators that fail the build. Threshold is the minimum mean score.
CRITICAL_EVALUATORS = ("routing_correct", "numbers_grounded", "no_unexpected_write")
PASS_THRESHOLD = float(os.environ.get("EVAL_PASS_THRESHOLD", "1.0"))

_tracing_started = False


def init_tracing(project_name: str | None = None):
    """Register the Phoenix tracer provider and instrument LangChain.

    MUST be called before anything imports ``orchestrator`` (which imports
    LangChain), exactly like chainlit_app.py does it.
    """
    global _tracing_started
    if _tracing_started:
        return None

    from phoenix.otel import register
    from openinference.instrumentation.langchain import LangChainInstrumentor

    provider = register(
        project_name=project_name or EVAL_PROJECT_NAME,
        endpoint=PHOENIX_ENDPOINT,
    )
    LangChainInstrumentor().instrument(tracer_provider=provider)
    _tracing_started = True
    return provider


def phoenix_client():
    """Phoenix REST client, new API preferred, legacy accepted.

    arize-phoenix >= 11 dropped ``phoenix.Client`` in favour of
    ``phoenix.client.Client``, and the two expose different method surfaces —
    callers must branch on ``hasattr(client, "datasets")``. Uses
    PHOENIX_REST_BASE_URL, NOT the OTLP trace-export endpoint.
    """
    # phoenix.experiments.run_experiment reads this from the environment.
    os.environ.setdefault("PHOENIX_COLLECTOR_ENDPOINT", PHOENIX_REST_BASE_URL)

    try:
        from phoenix.client import Client as NewClient
    except ImportError:
        NewClient = None
    if NewClient is not None:
        return NewClient(base_url=PHOENIX_REST_BASE_URL)

    import phoenix as px

    legacy = getattr(px, "Client", None)
    if legacy is None:
        raise RuntimeError(
            "No usable Phoenix client: neither phoenix.client.Client nor "
            "phoenix.Client is available. Install/upgrade arize-phoenix.")
    return legacy(endpoint=PHOENIX_REST_BASE_URL)


def call_with_supported_kwargs(fn, **kwargs):
    """Call *fn* with only the kwargs it declares.

    Lets one call site cover both client generations by passing every known
    alias (``name``/``dataset_name``/``dataset``) and letting the signature
    decide which one is real.
    """
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(**kwargs)

    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**kwargs)
    return fn(**{k: v for k, v in kwargs.items() if k in params})


# ----------------------------------------------------------------- Fabric ----
def fabric_from_env(fabric_id: int | None = None) -> dict:
    """Build the fabric dict supervisor.prepare() expects.

    Mirrors chainlit_app._fabric_config so the harness talks to exactly the
    same APIC_CLIENT MCP server deployment the app would use. Falls back to the
    legacy single-fabric APIC_CLIENT_* variables.
    """
    fid = fabric_id if fabric_id is not None else int(os.environ.get("EVAL_FABRIC_ID", "1"))

    def g(suffix, default=None):
        return os.environ.get(f"FABRIC_{fid}_{suffix}", default)

    if g("APIC_CLIENT_SERVER") or g("NAME"):
        return {
            "id": fid,
            "name": g("NAME") or f"Fabric {fid}",
            "description": g("DESCRIPTION") or f"ZT APIC fabric {fid}",
            "icon": g("ICON"),
            "apic_client_python": g("APIC_CLIENT_PYTHON", ""),
            "apic_client_server": g("APIC_CLIENT_SERVER", ""),
        }

    return {
        "id": 0,
        "name": os.environ.get("DEFAULT_FABRIC_NAME", "Default Fabric"),
        "description": "Default APIC fabric (single-fabric configuration).",
        "icon": None,
        "apic_client_python": os.environ.get("APIC_CLIENT_PYTHON", ""),
        "apic_client_server": os.environ.get("APIC_CLIENT_SERVER", ""),
    }


EVAL_TEAM = os.environ.get("EVAL_TEAM", "")
EVAL_PROFILE = os.environ.get("EVAL_PROFILE", "")


# ------------------------------------------------------------------ Judge ----
def judge_config() -> dict:
    """LLM-as-judge wiring. Defaults to the same LiteLLM endpoint the agent uses.

    ``self_judging`` is True when no separate judge model is configured, which
    makes the judge score a weaker signal — the runner prints this.
    """
    model_under_test = os.environ.get("LITELLM_MODEL", "")
    judge_model = os.environ.get("EVAL_JUDGE_MODEL", model_under_test)
    return {
        "base_url": os.environ.get("EVAL_JUDGE_BASE_URL", os.environ.get("LITELLM_BASE_URL", "")),
        "api_key": os.environ.get("EVAL_JUDGE_API_KEY", os.environ.get("LITELLM_API_KEY", "")),
        "model": judge_model,
        "model_under_test": model_under_test,
        "self_judging": bool(judge_model) and judge_model == model_under_test,
    }
