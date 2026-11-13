from contextlib import asynccontextmanager
from threading import BoundedSemaphore

from fastapi import FastAPI, HTTPException

from rpi5_agent.config import Settings
from rpi5_agent.llm import RoutedSummarizer
from rpi5_agent.models import DiagnoseRequest, Diagnosis, LLMTarget, Scenario
from rpi5_agent.specification import FIXTURE_SPECIFICATION
from rpi5_agent.tools.collection import EvidenceProvider, allowed_tools
from rpi5_agent.verification import (
    CASE_CATALOG_ID,
    EXPECTED_CODES,
    FixtureVerification,
    verify_fixture,
)
from rpi5_agent.workflow import Summarizer, build_workflow


def create_app(
    settings: Settings | None = None,
    summarizer: Summarizer | None = None,
    *,
    provider: EvidenceProvider | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings if settings is not None else Settings()
        model = summarizer
        if config.llm_enabled and model is None:
            model = RoutedSummarizer(config)
        app.state.settings = config
        app.state.workflow = build_workflow(
            model, provider=provider, profile=config.diagnostic_profile
        )
        app.state.diagnosis_slot = BoundedSemaphore(1)
        yield

    app = FastAPI(title="RPi 5 Diagnostic Agent", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict:
        # Liveness only: this does not probe the model or any physical hardware.
        return {"status": "ok", "mode": "fixture", "llm_enabled": app.state.settings.llm_enabled}

    @app.get("/scenarios")
    def scenarios() -> dict:
        return {"mode": "fixture", "scenarios": [item.value for item in Scenario]}

    @app.get("/capabilities")
    def capabilities() -> dict:
        profile = app.state.settings.diagnostic_profile
        return {
            "mode": "fixture",
            "diagnostic_profile": profile,
            "allowed_read_tools": sorted(allowed_tools(profile)),
            "physical_collection": False,
            "control_tools": False,
            "project_focus": "known_equipment_verification",
            "assessment_specification_id": FIXTURE_SPECIFICATION.id,
            "fixture_verification": provider is None,
        }

    @app.get("/verification/specification")
    def verification_specification() -> dict:
        return {
            "available": provider is None,
            "specification": FIXTURE_SPECIFICATION.model_dump(mode="json"),
            "case_catalog_id": CASE_CATALOG_ID,
            "physical_validation": False,
            "cases": [
                {"case_id": case, "expected_finding_codes": sorted(expected)}
                for case, expected in EXPECTED_CODES.items()
            ],
        }

    def run_diagnosis(request: DiagnoseRequest) -> Diagnosis:
        if request.use_llm and not app.state.settings.llm_enabled:
            raise HTTPException(
                status_code=409, detail="Set LLM_ENABLED=true to request a model summary"
            )
        if request.llm_target == LLMTarget.EXTERNAL and not app.state.settings.external_llm_enabled:
            raise HTTPException(status_code=409, detail="Set EXTERNAL_LLM_ENABLED=true first")
        # One diagnostic run at a time; do not queue unbounded model requests on the Pi.
        if not app.state.diagnosis_slot.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="A diagnosis is already running")
        try:
            return app.state.workflow.invoke({"request": request})["result"]
        finally:
            app.state.diagnosis_slot.release()

    @app.post("/diagnose", response_model=Diagnosis)
    def diagnose(request: DiagnoseRequest) -> Diagnosis:
        return run_diagnosis(request)

    @app.post("/verify", response_model=FixtureVerification)
    def verify(request: DiagnoseRequest) -> FixtureVerification:
        # A custom provider has no reviewed acceptance suite. Never silently grade it
        # as the built-in fixture, even if it implements the same Python protocol.
        if provider is not None:
            raise HTTPException(status_code=409, detail="No acceptance suite for this provider")
        return verify_fixture(run_diagnosis(request))

    return app


app = create_app()
