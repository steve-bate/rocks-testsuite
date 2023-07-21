import asyncio
import logging
import uuid
from json import JSONDecodeError
from typing import Any, Awaitable, Callable

import httpx
from fastapi import Response

from rocks_testsuite.result import (
    TestFailure,
    TestInconclusive,
    TestNotApplicable,
    TestResults,
)

_logger = logging.getLogger("rocks.session")


async def _get_json(url: str, token: str | None = None):
    headers = {
        "Accept": "application/activity+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers=headers,
            timeout=None,
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

    async def get_json(self, url: str):
        return await _get_json(url, self.token)

    async def post_to_outbox(self, obj: dict, is_object: bool = False) -> Response:
        if "@context" not in obj:
            obj["@context"] = "https://www.w3.org/ns/activitystreams"
        if not is_object and "actor" not in obj:
            obj["actor"] = self.uri
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.profile["outbox"],
                json=obj,
                headers={
                    "Content-Type": "application/activity+json",
                    "Authorization": f"Bearer {self.token}",
                },
                timeout=None,
            )
            response.raise_for_status()
            return response


class C2SServerTests:
    def __init__(self, session, results: TestResults):
        self._session = session
        self._results = results

    async def run(self):
        _logger.info(f"Running {type(self).__name__}")
        await self.setup_client()
        await self.run_test(self.test_outbox_activity_posted)
        await self.run_test(self.test_outbox_removes_bto_and_bcc)
        await self.run_test(self.test_outbox_non_activity)
        await self.run_test(self.test_outbox_update)
        ## We HAVE these tests, but since they didn't make it into
        ## ActivityPub proper they're commented out of the test suite for now...
        # (test-outbox-upload-media case-worker)
        await self.run_test(self.test_outbox_activity_follow_undo)
        # (test-outbox-verification case-worker)
        # (test-outbox-subjective case-worker)
        await self.run_test(self.test_outbox_activity_create)
        await self.run_test(self.test_outbox_activity_add_remove)
        await self.run_test(self.test_outbox_activity_like)
        await self.run_test(self.test_outbox_activity_block)
        await self._session.ask_questions(
            "outbox-remaining-questions", "c2s-server-test-items"
        )

    async def run_test(self, test: Callable[[], Awaitable[TestResults]]):
        await self._session.send_notice_str(f"Running test: {test.__name__}")
        results = await test()
        _logger.info(f"Test {test.__name__}: id={self._session.id}, results={results}")
        await self._session.send_notice("results_table.jinja", {"items": results})
        self._results.update(results)

    @staticmethod
    def _get_uri(obj: dict | str) -> str | None:
        return obj.get("id") if isinstance(obj, dict) else obj

    @classmethod
    def _get_uris(cls, obj: dict, key: str):
        value = obj.get(key)
        if value:
            if not isinstance(value, list):
                value = [value]
            return sorted(cls._get_uri(v) for v in value)
        return value

    async def setup_client(self):
        actor_uri = None
        while True:
            answer = await self._session.send_question(
                "get_actor_uri.jinja",
                {"actor_uri": actor_uri},
            )
            actor_uri = answer["actor-id"]
            try:
                profile = await _get_json(actor_uri)
                break
            except JSONDecodeError:
                _logger.error(f"Failed to parse actor JSON-LD for {actor_uri}")
                await self._session.send_notice_str(
                    "<span class='result-log-fail'>Failed to "
                    "parse actor profile JSON-LD</span>"
                )
            except Exception:
                _logger.error("Failed to retrieve actor", exc_info=1)
                await self._session.send_notice_str(
                    "<span class='result-log-fail'>Failed to "
                    "retrieve actor profile</span>"
                )
        token = await self.get_auth_token(profile)
        self._apclient = APClient(profile, token)
        _logger.info(f"APClient created for {self._apclient.uri}")

    async def get_auth_token(self, profile: dict[str, Any]):
        auth_token_endpoint = None
        endpoints = profile.get("endpoints")
        if endpoints:
            auth_token_endpoint = endpoints.get("oauthAuthorizationEndpoint")
        answers = await self._session.send_question(
            "get_auth_token.jinja", {"auth_token_endpoint": auth_token_endpoint}
        )
        return answers["auth-token"]

    async def item_uris(
        self, collection_uri: str, max_count: int = 100, count: int = 0
    ):
        collection = await self._apclient.get_json(collection_uri)
        for item_key in ["items", "orderedItems"]:
            # We can't rely on the collection "type". It's compliant to
            # have a Collection with orderedItems or a collection with multiple
            # types or even a collection with both items and orderedItems.
            # It might be insane, but... that's a different discussion.
            if item_key in collection:
                items = collection[item_key]
                if not isinstance(items, list):
                    items = [items]
                for item in items:
                    item_uri = self._get_uri(item)
                    if item_uri is not None:
                        yield item_uri
                        count += 1
                        if count == max_count:
                            return
        for page_key in ["first", "next"]:
            if page_key in collection:
                page_uri = collection[page_key]
                async for item in self.item_uris(page_uri, max_count, count):
                    yield item
                    count += 1
                    if count == max_count:
                        return

    async def test_outbox_activity_posted(self) -> TestResults:
        results: TestResults = {}
        activity_submitted = None
        id_to_replace = "http://activitypub.test/act/foo-id-here/"
        response = await self._apclient.post_to_outbox(
            {
                "id": id_to_replace,
                "type": "Create",
                "object": {
                    "type": "Note",
                    "attributedTo": self._apclient.uri,
                    "content": "Up for some root beer floats?",
                },
            }
        )

        # [outbox:responds-201-created]
        if response.status_code == 201:
            self._results["outbox:responds-201-created"] = True
            activity_submitted = True
        else:
            self._results["outbox:responds-201-created"] = TestFailure(
                f"Responded with status code {response.status_code}"
            )

        # [outbox:location-header]
        if "Location" in response.headers:
            activity_submitted = True
            self._results["outbox:location-header"] = True
            activity_uri = response.headers["Location"]
            activity = await self._apclient.get_json(activity_uri)
            # make sure the id was changed for the outer activity
            results["outbox:ignores-id"] = activity.get("id") != id_to_replace
        else:
            results["outbox:location-header"] = False
            results["outbox:ignores-id"] = TestInconclusive("No Location in headers")

        results["outbox:accepts-activities"] = (
            True
            if activity_submitted
            else TestInconclusive(
                "Response code not 201 and no Location header present"
            )
        )

        return results

    async def test_outbox_removes_bto_and_bcc(self) -> TestResults:
        results: TestResults = {}
        actor_1 = await self._session.create_actor()
        actor_2 = await self._session.create_actor()
        response = await self._apclient.post_to_outbox(
            {
                "type": "Create",
                "bto": actor_1.uri,
                "bcc": actor_2.uri,
                # Must be public to be seen by third party
                "audience": "as:Public",
                "object": {
                    "type": "Note",
                    "attributedTo": self._apclient.uri,
                    "content": "Up for some root beer floats?",
                },
            }
        )
        activity_uri = response.headers.get("Location")
        if activity_uri:
            # activity = await self._apclient.get_json(activity_uri)
            # TODO SPEC It's not clear if an activity owner can see bto and bcc
            third_party = await self._session.create_actor()
            activity = await third_party.get_json(activity_uri)
            results["outbox:removes-bto-and-bcc"] = not (
                "bcc" in activity and "bto" in activity
            )
        else:
            results["outbox:removes-bto-and-bcc"] = TestInconclusive(
                "No Location header in response"
            )
        return results

    async def test_outbox_non_activity(self) -> TestResults:
        results: TestResults = {}
        note = {"type": "Note", "content": "Up for some root beer floats?"}
        response = await self._apclient.post_to_outbox(note, is_object=True)
        activity_uri = response.headers.get("Location")
        if activity_uri:
            activity = await self._apclient.get_json(activity_uri)
            results["outbox:accepts-non-activity-objects"] = (
                True
                if activity.get("type") == "Create"
                else TestFailure(
                    "ActivityStreams object pointed to by "
                    "response Location is not of type Create"
                )
            )
        else:
            results["outbox:removes-bto-and-bcc"] = TestInconclusive(
                "No Location header in response"
            )
        return results

    async def test_outbox_update(self) -> TestResults:
        results: TestResults = {}
        # Submit original activity
        response = await self._apclient.post_to_outbox(
            {
                "type": "Create",
                "object": {
                    "type": "Note",
                    "attributedTo": self._apclient.uri,
                    "name": "An indecisive note",
                    "content": "I'm feeling indecisive!",
                },
            }
        )

        activity_uri = response.headers.get("Location")
        if activity_uri:
            original_activity = await self._apclient.get_json(activity_uri)
            object_uri = self._get_uri(original_activity["object"])
        else:
            results["outbox:update"] = TestFailure("No Location header in response")
            return results

        # Submit updated activity - remove name, update content
        response = await self._apclient.post_to_outbox(
            {
                "type": "Update",
                "object": {
                    "id": object_uri,
                    "type": "Note",
                    "attributedTo": self._apclient.uri,
                    "name": None,
                    "content": "I've changed my mind!",
                },
            }
        )

        updated_object = await self._apclient.get_json(object_uri)

        if updated_object.get("content") != "I've changed my mind!":
            results["outbox:update"] = TestFailure(
                "Failed to update field with replacement data"
            )
            return results
        elif "name" in updated_object:
            results["outbox:update"] = TestFailure(
                "Unable to delete field by passing an Update with null value"
            )
            return results

        # Submit another update - add back name with different content
        response = await self._apclient.post_to_outbox(
            {
                "type": "Update",
                "object": {
                    "id": object_uri,
                    "type": "Note",
                    "attributedTo": self._apclient.uri,
                    "name": "new name, same flavor",
                },
            }
        )

        updated_object = await self._apclient.get_json(object_uri)

        if updated_object.get("content") != "I've changed my mind!":
            results["outbox:update"] = TestFailure(
                "Field changed, despite not being included in update"
            )
            return results
        elif updated_object.get("name") != "new name, same flavor":
            results["outbox:update"] = TestFailure(
                "Failed to update field with replacement data"
            )
            return results

        results["outbox:update"] = True
        return results

    async def test_outbox_activity_follow_undo(self) -> TestResults:
        results: TestResults = {}
        actor = await self._session.create_actor()
        response = await self._apclient.post_to_outbox(
            {
                "type": "Follow",
                "to": actor.uri,
                "object": actor.uri,
            }
        )

        follow_activity_uri = response.headers.get("Location")

        if follow_activity_uri:
            results["outbox:follow"] = True
            # The original case seems to make some assumptions about accept behavior (?)

            async def actor_is_following():
                async for uri in self.item_uris(self._apclient.profile["following"]):
                    if actor.uri == uri:
                        return True
                return False

            # It can take a while for the Follow to be processed
            is_following = False
            for _ in range(5):
                is_following = await actor_is_following()
                if is_following:
                    break
                await asyncio.sleep(1)

            results["outbox:follow:adds-followed-object"] = is_following
            if is_following:
                response = await self._apclient.post_to_outbox(
                    {
                        "type": "Undo",
                        "object": follow_activity_uri,
                    }
                )
                # if_following is True here
                for _ in range(5):
                    is_following = await actor_is_following()
                    if not is_following:
                        break
                    await asyncio.sleep(1)
                results["outbox:undo"] = not is_following
            else:
                results["outbox:undo"] = TestInconclusive("Actor not followed")
        else:
            results["outbox:follow"] = False
            results["outbox:undo"] = TestInconclusive("Follow activity posting failed")

        return results

    async def test_outbox_activity_create(self):
        results: TestResults = {}
        actor1 = await self._session.create_actor()
        actor2 = await self._session.create_actor()
        actor3 = await self._session.create_actor()
        actor4 = await self._session.create_actor()
        actor5 = await self._session.create_actor()

        response = await self._apclient.post_to_outbox(
            {
                "type": "Create",
                "to": [actor1.uri, actor2.uri],
                "cc": actor3.uri,
                "object": {
                    "type": "Note",
                    "cc": [actor4.uri, actor5.uri],
                    "content": "Hi there!",
                },
            }
        )

        activity_uri = response.headers.get("Location")
        if activity_uri:
            results["outbox:create"] = True
            activity = await self._apclient.get_json(activity_uri)
            object_ = activity["object"]
            expected_to = sorted([actor1.uri, actor2.uri])
            expected_cc = sorted([actor3.uri, actor4.uri, actor5.uri])
            results["outbox:create:merges-audience-properties"] = (
                self._get_uris(activity, "to") == expected_to
                and self._get_uris(activity, "cc") == expected_cc
                and self._get_uris(object_, "to") == expected_to
                and self._get_uris(object_, "cc") == expected_cc
            )
            results["outbox:create:actor-to-attributed-to"] = object_.get(
                "attributedTo"
            ) == activity.get("actor")

        else:
            results["outbox:create"] = False
            results["outbox:create:merges-audience-properties"] = TestInconclusive(
                "Create failed"
            )
            results["outbox:create:actor-to-attributed-to"] = TestInconclusive(
                "Create failed"
            )

        return results

    async def test_outbox_activity_like(self):
        results: TestResults = {}

        if "liked" not in self._apclient.profile:
            # Server doesn't support liked collections
            inconclusive = TestNotApplicable("Like not supported")
            results["outbox:like"] = inconclusive
            results["outbox:like:adds-object-to-liked"] = inconclusive
            return results

        # Create a note to like
        response = await self._apclient.post_to_outbox(
            {
                "type": "Create",
                "object": {
                    "type": "Note",
                    "content": "A very likable post!",
                },
            }
        )

        create_activity_uri = response.headers.get("Location")
        if create_activity_uri:
            results["outbox:create"] = True
            create_activity = await self._apclient.get_json(create_activity_uri)
            likable_note = create_activity["object"]
            likable_note_uri = self._get_uri(likable_note)
            # Like the note
            response = await self._apclient.post_to_outbox(
                {
                    "type": "Like",
                    "object": likable_note_uri,
                }
            )
            if "Location" in response.headers:
                results["outbox:like"] = True
            # Check if liked in collection
            # TODO handle optional liked collection
            liked_uri = self._apclient.profile["liked"]
            note_liked = False
            async for uri in self.item_uris(liked_uri):
                if likable_note_uri == uri:
                    note_liked = True
                    break
            results["outbox:like:adds-object-to-liked"] = note_liked
        else:
            results["outbox:create"] = False
            inconclusive = TestInconclusive("Setup failed (note creation)")
            results["outbox:like"] = inconclusive
            results["outbox:like:adds-object-to-liked"] = inconclusive

        return results

    async def test_outbox_activity_block(self):
        results: TestResults = {}
        obnoxious_actor = await self._session.create_actor()

        # Block the bad actor
        response = await self._apclient.post_to_outbox(
            {
                "type": "Block",
                "object": obnoxious_actor.uri,
            }
        )

        block_activity_uri = response.headers.get("Location")
        if block_activity_uri:
            results["outbox:block"] = True
            # Try to post a new note to inbox
            response = await obnoxious_actor.post(
                self._apclient.profile["inbox"],
                {
                    "object": {
                        "type": "Note",
                        "content": "Well, actually...",
                        # Inbox implies the "to"
                        # "to": self._apclient.uri,
                        # Should block even without this
                        # "attributedTo": obnoxious_actor.uri,
                    }
                },
            )
            # TODO could this be masked by authentication issues?
            results[
                "outbox:block:prevent-interaction-with-actor"
            ] = response.status_code in [403, 405]
        else:
            results["outbox:block"] = False
            results["outbox:block:prevent-interaction-with-actor"] = TestInconclusive(
                "Block post didn't succeed"
            )

        return results

    async def test_outbox_activity_add_remove(self):
        results: TestResults = {}

        # Create a collection
        # Many server don't support this
        response = await self._apclient.post_to_outbox(
            {
                "type": "Create",
                "object": {
                    "type": "Collection",
                    "name": "test collection " + uuid.uuid4().hex,
                },
            }
        )

        collection_create_activity_uri = response.headers.get("Location")
        if collection_create_activity_uri:
            # Create a note
            response = await self._apclient.post_to_outbox(
                {
                    "type": "Create",
                    "object": {
                        "type": "Note",
                        "name": "I'm a note",
                    },
                }
            )
            note_create_activity_uri = response.headers.get("Location")
            note = await self._apclient.get_json(note_create_activity_uri)
            note_uri = self._get_uri(note)
            if note_create_activity_uri:
                # Add the note to the collection
                collection_create_activity = await self._apclient.get_json(
                    collection_create_activity_uri
                )
                collection_uri = self._get_uri(collection_create_activity["object"])
                response = await self._apclient.post_to_outbox(
                    {
                        "type": "Add",
                        # TODO SPEC Can't one add an existing note
                        # Wouldn't the identifier be replaced?
                        "object": note_uri,
                        "target": collection_uri,
                    }
                )
                add_activity_uri = response.headers.get("Location")
                if add_activity_uri:
                    results["outbox:add"] = True
                    note_was_added = False
                    async for uri in self.item_uris(collection_uri):
                        if note_uri == uri:
                            note_was_added = True
                            break
                    results["outbox:add:adds-object-to-target"] = note_was_added
                    if note_was_added:
                        # Remove the item from the collection
                        response = await self._apclient.post_to_outbox(
                            {
                                "type": "Remove",
                                # TODO SPEC We obviously need to specify a URI here
                                # Wouldn't the identifier be replaced?
                                "object": note_uri,
                                "target": collection_uri,
                            }
                        )
                        remove_activity_uri = response.headers.get("Location")
                        if remove_activity_uri:
                            results["outbox:remove"] = True
                            # Unfortunately, the negative test requires reading the full
                            # collection up to the max count constraint.
                            results[
                                "outbox:remove:removes-from-target"
                            ] = note_uri not in [
                                self._get_uri(i)
                                async for i in self.item_uris(collection_uri)
                            ]
                        else:
                            results["outbox:remove"] = False
                            results["outbox:add:removes-from-target"] = False
                else:
                    results["outbox:add"] = False
                    results["outbox:add:adds-object-to-target"] = False
                    results["outbox:remove"] = TestInconclusive("Add failed")
                    results["outbox:remove:removes-from-target"] = TestInconclusive(
                        "Add failed"
                    )
        return results
