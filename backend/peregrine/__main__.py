import uvicorn

from . import GATEWAY_PORT, LOGS, MODELS

for d in (MODELS, LOGS):
    d.mkdir(parents=True, exist_ok=True)
uvicorn.run("peregrine.api:app", host="127.0.0.1", port=GATEWAY_PORT, log_level="info")
