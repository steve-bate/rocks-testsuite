import collections
import json
import logging
import os
import uuid
from datetime import datetime
from functools import lru_cache
from typing import Any, Tuple

import httpx
import jinja2
from cryptography.hazmat.backends import default_backend as crypto_default_backend
from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from rocks_testsuite.c2s_tests import C2SServerTests
from rocks_testsuite.result import ResultCode
from rocks_testsuite.signatures import HttpSignatureAuth

_logger = logging.getLogger("rocks.session")


class TestSessionManager:
    def __init__(self):
        self.sessions: dict[str, "TestSession"] = {}


ResultsType = dict[str, dict[str, bool | ResultCode]]


class TestSession:
    def __init__(self, websocket: WebSocket, env: jinja2.Environment, config: dict):
        self.id = uuid.uuid4().hex
        self.websocket = websocket
        self._templates = env
        self.results: ResultsType = collections.defaultdict(dict)
        self.metadata, self.questionnaire = self._load_test_data()
        self.actors: dict[str, "TestActor"] = {}
        self.config: dict[str, Any] = config or {}
        _logger.info(
            f"Test session created: id={self.id}, "
            + f"remote_addr={websocket.client.host}, "
            + f"forwarded={websocket.headers.get('X-Forwarded-For')}, "
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
                self.config.update(await self.send_question("setup.jinja"))
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
        context = context or {}
        if "session" not in context:
            context["session"] = self
        content = self._templates.get_template(template_name).render(context)
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
        context = context or {}
        if "session" not in context:
            context["session"] = self
        content = self._templates.get_template(template_name).render(context)
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


# Use same keys for all test actors
@lru_cache
def get_key_pair() -> Tuple[str, str]:
    pair = rsa.generate_private_key(
        backend=crypto_default_backend(), public_exponent=65537, key_size=2048
    )
    return (
        pair.public_key()
        .public_bytes(
            crypto_serialization.Encoding.PEM,
            crypto_serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
        pair.private_bytes(
            crypto_serialization.Encoding.PEM,
            crypto_serialization.PrivateFormat.PKCS8,
            # FIXME support encryption with a configured passphrase
            crypto_serialization.NoEncryption(),
        ).decode(),
    )


class TestActor:
    _id_counter = 1

    def __init__(self, session: TestSession, uri: str):
        self.session = session
        self.uri = uri
        public_key, private_key = get_key_pair()
        key_id = f"{uri}#main-key"
        self.profile = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": uri,
            "preferredUsername": f"actor-{TestActor._id_counter}",
            "inbox": f"{uri}/inbox",
            "outbox": f"{uri}/outbox",
            "publicKey": {
                "id": key_id,
                "owner": uri,
                "publicKeyPem": public_key,
            },
        }
        self.auth = HttpSignatureAuth(key_id, private_key)
        self.private_key = private_key
        TestActor._id_counter += 1
        self.inbox: list[dict[str, Any]] = []

    async def process_request(self, request: Request) -> Response:
        path = request.path_params.get("path")
        if path == "" or path is None:
            return JSONResponse(self.profile, media_type="application/activity+json")
        elif path == "inbox":
            activity = await request.json()
            self.inbox.append(activity)
            # Auto accept follow
            if activity["type"] == "Follow":
                async with httpx.AsyncClient() as client:
                    # get following actor
                    response = await client.get(
                        activity["actor"],
                        headers={"Accept": "application/activity+json"},
                        timeout=None,
                        auth=self.auth,
                    )
                    response.raise_for_status()
                    following_actor = response.json()
                    response = await self.post(
                        following_actor["inbox"],
                        {
                            "@context": "https://www.w3.org/ns/activitystreams",
                            "id": f"{self.uri}/accept-{uuid.uuid4()}",
                            "type": "Accept",
                            "actor": self.uri,
                            "object": activity["id"],
                        },
                    )
                    if response.is_success:
                        _logger.info(f"Accept sent: session={self.session.id}")
                    else:
                        _logger.error(
                            "Sending accept response failed: "
                            f"{response.status_code} {response.reason_phrase}"
                        )
            return Response("Accepted", 202)
        else:
            raise HTTPException(404, "Actor path not found")

    async def get_json(self, url: str):
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers={"Accept": "application/activity+json"},
                auth=self.auth,
                timeout=None,
            )
            response.raise_for_status()
            return response.json()

    async def post(self, url: str, json_data: dict[str, Any]):
        if "id" not in json_data:
            json_data["id"] = f"{self.uri}/accept-{uuid.uuid4()}"
        if "actor" not in json_data:
            json_data["actor"] = self.uri
        async with httpx.AsyncClient() as client:
            return await client.post(
                url,
                json=json_data,
                headers={"Content-Type": "application/activity+json"},
                auth=self.auth,
                timeout=None,
            )
