"""ERB Matching Service: wagon-to-order assignment optimization."""
from fastapi import FastAPI
from matching.models import MatchRequest, MatchResponse
from matching import match

app = FastAPI(
    title="ERB Matching Service",
    version="0.1.0",
    description="Wagon-to-order matching algorithm for Empty Run Buster",
)


@app.post(
    "/api/match",
    response_model=MatchResponse,
    summary="Match idle wagons to pending orders",
    description=(
        "Accepts the full system state (orders, wagons, stations, edges) "
        "and returns optimal wagon assignments minimizing total empty run distance. "
        "Includes naive baseline comparison and cost metrics."
    ),
)
def api_match(request: MatchRequest) -> MatchResponse:
    return match(request)


@app.get("/health", summary="Health check")
def health():
    return {"status": "ok"}
