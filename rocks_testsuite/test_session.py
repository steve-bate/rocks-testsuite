import collections
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Tuple

import jinja2
from fastapi import HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from rocks_testsuite.c2s_tests import C2SServerTests
from rocks_testsuite.result import ResultCode

_logger = logging.getLogger("rocks.session")


class TestSessionManager:
    def __init__(self):
        self.sessions: dict[str, "TestSession"] = {}


ResultsType = dict[str, dict[str, bool | ResultCode]]


class TestSession:
    def __init__(self, websocket: WebSocket, env: jinja2.Environment):
        self.id = uuid.uuid4().hex
        self.websocket = websocket
        self._templates = env
        self.results: ResultsType = collections.defaultdict(dict)
        self.metadata, self.questionnaire = self._load_test_data()
        self.actors: dict[str, "TestActor"] = {}
        # This will be initialized before usage
        self.config: dict[str, Any] = None  # type: ignore
        _logger.info(
            f"Test session created: id={self.id}, "
            + f"remote_addr={websocket.client.host}, "
            + f"fowarded={websocket.headers.get('X-Forwarded-For')}, "
            + f"user-agent={websocket.headers.get('User-Agent')} "
        )

    def _load_data(self, filename: str):
        filepath = os.path.join(os.path.dirname(__file__), "data", filename)
        with open(filepath) as fp:
            return json.load(fp)

    def _load_test_data(self) -> Tuple[dict[str, Any], dict[str, Any]]:
        return (
            {entry["id"]: entry for entry in self._load_data("test_metadata.json")},
            self._load_data("questionnaire.json"),
        )

    def get_questions(self, group_name: str) -> list[dict[str, Any]]:
        return self.questionnaire[group_name]

    async def run(self):
        try:
            await self.send_notice("greeting.jinja")
            while True:
                self.config = await self.send_question("setup.jinja")
                # This validation could be done in the browser,
                # but this was the original way
                if any(
                    self.config[key]
                    for key in [
                        "testing-client",
                        "testing-c2s-server",
                        "testing-s2s-server",
                    ]
                ):
                    break
                await self.send_notice_str(
                    "It looks like you didn't select anything. "
                    "Please select at least one implementation type to test."
                )
            # TODO: support verbose-debugging option?
            if self.config.get("testing-client"):
                await self.run_client_tests()
            if self.config.get("testing-c2s-server"):
                case = C2SServerTests(self, self.results["c2s-server-test-items"])
                await case.run()
            if self.config.get("testing-s2s-server"):
                await self.run_s2s_tests()
            if self.config.get("testing-c2s-server") or self.config.get(
                "testing-s2s-server"
            ):
                await self.run_server_common_tests()
            _logger.info("Tests complete. Querying project information.")
            project_info = await self.get_project_info()
            await self.save_report(project_info)
            await self.send_notice(
                "report.jinja", {"report_link": f"/download-report/{self.id}.json"}
            )
            await self.send_question("finish.jinja")
        except WebSocketDisconnect:
            pass

    async def save_report(self, project_info: dict[str, Any]):
        report = dict(project_info)
        report["date"] = datetime.now().isoformat()
        report.update(self.config)
        report["results"] = self.results
        filepath = os.path.join(os.path.dirname(__file__), "reports", f"{self.id}.json")
        with open(filepath, "w") as fp:
            json.dump(report, fp, indent=2)
        return f"/download-report/{self.id}"

    async def get_project_info(self):
        answers = {}
        message = None
        while True:
            answers = await self.send_question(
                "project_info.jinja",
                {
                    "config": self.config,
                    "results": self.results,
                    "metadata": self.metadata,
                    "answers": answers,
                    "message": message,
                },
            )
            if all(
                key in answers and answers[key] != ""
                for key in ["project-name", "website", "repo"]
            ):
                break
            message = (
                "<h3 style='color: red'>Please include at least project, "
                + "website and repo information.</h3>"
            )
        return answers

    async def send_notice(
        self, template_name: str, context: dict[str, Any] | None = None
    ):
        content = self._templates.get_template(template_name).render(context or {})
        await self.websocket.send_json(
            {
                "type": "notice",
                "content": content,
            }
        )

    async def send_notice_str(self, content: str):
        await self.websocket.send_json(
            {
                "type": "notice",
                "content": content,
            }
        )

    async def send_question(
        self,
        template_name: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content = self._templates.get_template(template_name).render(context or {})
        answers = await self.send_question_str(content)
        _logger.info(
            f"Question: id={self.id}, template={template_name}, answers={answers}"
        )
        return answers

    async def send_question_str(
        self,
        content: str,
    ) -> dict[str, Any]:
        await self.websocket.send_json(
            {
                "type": "input-prompt",
                "content": content,
                "can-go-back": False,
            }
        )
        answer = await self.websocket.receive_json()
        if "data" not in answer:
            raise HTTPException(500, detail="Missing 'data' property in answer")
        return answer["data"]

    async def create_actor(self) -> "TestActor":
        # create uri from websocket?
        url = self.websocket.base_url
        # TODO The request info for reverse-proxy WS is not clear
        actor_id = uuid.uuid4().hex
        actor_uri = (
            ("http" if url.scheme == "ws" else "https")
            + "://"
            + url.netloc
            + url.path
            + "ap/u/"
            + self.id
            + "/"
            + actor_id
        )
        actor = TestActor(self, actor_uri)
        self.actors[actor_id] = actor
        return actor

    async def process_actor_request(self, request: Request) -> Response:
        actor = self.actors.get(request.path_params["actor_id"])
        if actor:
            return await actor.process_request(request)
        else:
            raise HTTPException(404, "Actor not found")

    async def run_client_tests(self):
        await self.send_notice_str(self._center("<h2>Client tests...</h2>"))
        await self.ask_questions("client-test-items")

    async def run_s2s_tests(self):
        await self.send_notice_str(
            self._center(
                "<h2>Testing server's federation / server-to-server support...</h2>"
            )
        )

        if self.config.get("testing-c2s-server"):
            await self.ask_questions(
                "server-inbox-delivery-c2s", "server-inbox-test-items"
            )

        await self.ask_questions("server-inbox-delivery", "server-inbox-test-items")

        await self.send_notice_str(
            self._center("<h3>Tests for receiving objects to inbox</h3>")
        )

        await self.ask_questions("server-inbox-accept", "server-inbox-test-items")

    async def run_server_common_tests(self):
        await self.send_notice_str(self._center("<h2>Common server tests...</h2>"))
        await self.ask_questions("server-common-test-items")

    @staticmethod
    def _center(s: str):
        return f'<div style="text-align: center;">{s}</div>'

    async def ask_questions(
        self, question_group_name: str, result_group_name: str | None = None
    ):
        results = self.results[result_group_name or question_group_name]
        for group in self.get_questions(question_group_name):
            answer = await self.send_question("questions.jinja", group)
            results.update(answer)
        _logger.info(f"Question group: id={self.id}, results={results}")


class TestActor:
    _id_counter = 1

    def __init__(self, session: TestSession, uri: str):
        self.session = session
        self.uri = uri
        self.profile = {
            "id": uri,
            "preferredUsername": f"actor-{TestActor._id_counter}",
            "inbox": f"{uri}/inbox",
            "outbox": f"{uri}/outbox",
        }
        TestActor._id_counter += 1
        self.inbox: list[dict[str, Any]] = []

    async def process_request(self, request: Request) -> Response:
        path = request.path_params["path"]
        if path == "":
            return JSONResponse(self.profile, media_type="application/activity+json")
        elif path == "inbox":
            activity = await request.json()
            self.inbox.append(activity)
            # Auto accept follow
            if activity["type"] == "Follow":
                _logger.warn("Accept/Follow not implemented")
                # async with httpx.AsyncClient() as client:
                #     # get following actor
                #     response = await client.get(
                #         activity["actor"],
                #         headers={"Accept": "application/activity+json"},
                #         timeout=None,
                #     )
                #     response.raise_on_status()
                #     following_actor = response.json()
                #     # TODO boo... this is going to need http sigs, etc.
            return Response("Accepted", 202)
        else:
            raise HTTPException(404, "Actor path not found")
