from contextlib import asynccontextmanager
from threading import BoundedSemaphore

from fastapi import FastAPI, HTTPException

from rpi5_agent.config import Settings
from rpi5_agent.llm import RoutedSummarizer
from rpi5_agent.models import DiagnoseRequest, Diagnosis, LLMTarget, Scenario
from rpi5_agent.workflow import Summarizer, build_workflow


def create_app(settings: Settings | None = None, summarizer: Summarizer | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings if settings is not None else Settings()
        model = summarizer
        if config.llm_enabled and model is None:
            model = RoutedSummarizer(config)
        app.state.settings = config
        app.state.workflow = build_workflow(model)
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

    @app.post("/diagnose", response_model=Diagnosis)
    def diagnose(request: DiagnoseRequest) -> Diagnosis:
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

    return app


app = create_app()
