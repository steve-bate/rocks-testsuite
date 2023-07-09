import argparse
import json
import logging
import os
from contextlib import asynccontextmanager

import coloredlogs
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from rocks_testsuite.test_session import TestSession, TestSessionManager

_logger = logging.getLogger("rocks.app")

base_dir = os.path.dirname(__file__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # logging needs to be configured here for proper reload behavior
    log_level = os.environ["TESTSUITE_LOG_LEVEL"] or logging.INFO
    _setup_logging(log_level)

    # This might (probably will) change later
    config_file = os.environ.get("TESTSUITE_CONFIG")
    if config_file:
        with open(config_file) as fp:
            config = json.load(fp)
    else:
        config = {}

    app.state.config = config
    app.state.session_manager = TestSessionManager()
    yield


app = FastAPI(lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=f"{base_dir}/static"),
    name="static",
)

app.mount(
    "/download-report",
    StaticFiles(directory=f"{base_dir}/reports"),
    name="reports",
)

templates = Jinja2Templates(directory=f"{base_dir}/templates")


@app.get("/")
def app_page(request: Request):
    return templates.TemplateResponse("app.jinja", {"request": request})


# emulating activitypub actors and endpoints
@app.route("/ap/u/{session_id}/{actor_id}/{path:path}", methods=["GET", "POST"])
async def activitypub(request: Request) -> Response:
    _logger.info(f"ActivityPub request: {request}, {request.path_params}")
    session = request.app.state.session_manager.sessions.get(
        request.path_params["session_id"]
    )
    if session:
        return await session.process_actor_request(request)
    else:
        raise HTTPException(404, detail="Unknown test session")


@app.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    state = websocket.app.state
    session = TestSession(websocket, templates.env, state.config)
    try:
        state.session_manager.sessions[session.id] = session
        await session.run()
    finally:
        del state.session_manager.sessions[session.id]


def _setup_logging(level_name: int | str):
    # Hack to avoid silly uvicorn.error level naming
    logging.root.setLevel(logging.getLevelName(level_name))
    logging.getLogger("uvicorn.error").name = "uvicorn"
    coloredlogs.install(
        milliseconds=True,
        level=level_name,
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config", help="optional config file")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Reload app when files change",
    )
    parser.add_argument(
        "--log_level",
        type=str.upper,
        choices=["NOTSET", "DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"],
        default="INFO",
    )
    args = parser.parse_args()
    if args.reload:
        args.reload_includes = [
            "rocks_testsuite/data",
            "rocks_testsuite/templates",
            "rocks_testsuite/static",
        ]

    os.environ["TESTSUITE_CONFIG"] = args.config
    # Passing log level via env for reload behavior
    os.environ["TESTSUITE_LOG_LEVEL"] = args.log_level
    _setup_logging(args.log_level)
    _logger.info("ActivityPub test suite")
    uvicorn.run(
        "rocks_testsuite.app:app",
        log_config=None,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_includes=args.reload_includes,
        log_level=args.log_level.lower(),
    )


if __name__ == "__main__":
    main()
