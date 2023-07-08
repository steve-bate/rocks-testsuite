import argparse
import logging
import os
from contextlib import asynccontextmanager

import coloredlogs
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from rocks_testsuite.test_session import TestSession

_logger = logging.getLogger("rocks.app")

base_dir = os.path.dirname(__file__)


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # logging needs to be configured here for proper reload behavior
    log_level = os.environ["TESTSUITE_LOG_LEVEL"] or logging.INFO
    _setup_logging(log_level)
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
@app.get("/ap/u/{db_key}/{client_id}/{pseudo_actor}/{suffix:path}")
def activitypub(client_id: str, pseudo_actor: str, suffix: str):
    print("activitypub", client_id, pseudo_actor, suffix)


@app.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await TestSession(websocket, templates.env).run()


def _setup_logging(level_name: int | str):
    # Hack to avoid silly uvicorn.error level naming
    logging.root.setLevel(logging.getLevelName(level_name))
    logging.getLogger("uvicorn.error").name = "uvicorn"
    coloredlogs.install(milliseconds=True, level=level_name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--reload", action="store_true", help="Reload app when files change"
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
    # Passing log level via env for reload behavior
    os.environ["TESTSUITE_LOG_LEVEL"] = args.log_level
    _setup_logging(args.log_level)
    # uvicorn wants lowercase level?
    args.log_level = args.log_level.lower()
    _logger.info("ActivityPub test suite")
    uvicorn.run(
        "rocks_testsuite.app:app",
        log_config=None,
        **args.__dict__,
    )
