import logging
from typing import Any

import httpx
from fastapi import Response

from rocks_testsuite.util import TestFailure, TestInconclusive

_logger = logging.getLogger("rocks.session")


async def _get_json(url: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers={
                "Accept": "application/activity+json",
            },
        )
        response.raise_for_status()
        return response.json()


class APClient:
    """A simulated client for testing."""

    def __init__(self, profile: dict[str, Any], token: str):
        self.profile = profile
        self.token = token

    @property
    def uri(self):
        return self.profile["id"]

    async def post_to_outbox(self, obj: dict) -> Response:
        async with httpx.AsyncClient() as client:
            return client.post(
                self.profile["outbox"],
                headers={
                    "Content-Type": "application/activity+json",
                    "Authorization": f"Bearer {self.token}",
                },
            )


class C2SServerTests:
    def __init__(self, worker):
        self._worker = worker

    async def run(self):
        _logger.info(f"Running {type(self).__name__}")
        await self.setup_actor()
        await self.test_outbox_activity_posted()

    async def setup_actor(self):
        answer = await self._worker.send_question("c2s_auth.jinja")
        profile = _get_json(answer["actor-id"])
        token = await self.get_auth_token()
        self._apclient = APClient(profile, token)
        _logger.info(f"APClient created for {self._apclient.uri}")

    async def get_auth_token(self):
        auth_token_endpoint = None
        endpoints = self._apclient.profile.get("endpoints")
        if endpoints:
            auth_token_endpoint = endpoints.get("getAuthToken")
        if auth_token_endpoint:
            prompt = (
                "<p>Visit <a href='{auth_token_endpoint}'>this link</a> "
                + "and fill in the auth token:"
            )
        else:
            prompt = (
                "Unable to find OAuth endpoints... please"
                + " manually insert the auth token:"
            )
        await self._worker.send_question_str(
            f"""
{prompt}
<dl>
    <dt><b>Auth token</b></dt>
    <dd><input type="text" name="auth-token"></dd>
</dl>"""
        )

    async def test_outbox_activity_posted(self):
        activity_submitted = None
        id_to_replace = "http://tsyesika.co.uk/act/foo-id-here/"
        response = self._apclient.post_to_outbox(
            {
                "id": id_to_replace,
                "actor": self._apclient.uri,
                "object": {
                    "type": "Note",
                    "attributedTo": self._apclient.uri,
                    "content": "Up for some root beer floats?",
                },
            }
        )

        # [outbox:responds-201-created]
        if response.status_code == 201:
            self.report["outbox:responds-201-created"] = True
            activity_submitted = True
        else:
            self.report["outbox:responds-201-created"] = TestFailure(
                f"Responded with status code {response.status_code}"
            )

        # [outbox:location-header]
        if "Location" in response.headers:
            activity_submitted = True
            self.report["outbox:location-header"] = True
            object_uri = response.headers["Location"]
            object_ = _get_json(object_uri)
            # make sure the id was changed for the outer activity
            self.report["outbox:ignores-id"] = object_.get("id") != id_to_replace
        else:
            self.report["outbox:location-header"] = True
            self.report["outbox:ignores-id"] = TestInconclusive(
                "No Location in headers"
            )

        self.report["outbox:accepts-activities"] = (
            True
            if activity_submitted
            else TestInconclusive(
                "Response code not 201 and no Location header present"
            )
        )
