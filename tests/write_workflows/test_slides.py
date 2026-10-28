from __future__ import annotations

import copy

import pytest


@pytest.fixture
def workflow(load_workflow):
    return load_workflow("slides-workflow", "slides_workflow")


@pytest.fixture
def deck():
    return {
        "revisionId": "revision-1",
        "slides": [
            {
                "objectId": "slide_one",
                "pageElements": [{"objectId": "shape_one", "shape": {"text": {}}}],
            },
            {"objectId": "slide_two", "pageElements": []},
        ],
    }


def test_template_copy_keeps_destination_explicit_and_reports_created_id(workflow, fake_api):
    api = fake_api([{"id": "new_deck"}])
    result = workflow.run(
        {
            "operation": "copy-template",
            "body": {"template_id": "template", "title": "Disposable", "folder_id": "folder"},
        },
        "synthetic",
        lambda *_: api,
    )
    assert api.calls == [
        (
            "POST",
            "/drive/v3/files/template/copy?supportsAllDrives=true",
            {"name": "Disposable", "parents": ["folder"]},
        )
    ]
    assert result["deck_id"] == "new_deck" and "incomplete" in result["review"]


def test_batch_final_slide_deletion_cannot_bypass_refusal(workflow, deck, fake_api):
    api = fake_api([deck])
    with pytest.raises(workflow.Failure, match="final slide"):
        workflow.run(
            {
                "operation": "batch",
                "deck_id": "explicit",
                "body": {
                    "requests": [
                        {"deleteObject": {"objectId": "slide_one"}},
                        {"deleteObject": {"objectId": "slide_two"}},
                    ]
                },
            },
            "synthetic",
            lambda *_: api,
        )
    assert len(api.calls) == 1 and api.calls[0][0] == "GET"


def test_batch_validates_new_ids_and_uses_revision(workflow, deck, fake_api):
    api = fake_api([deck, {"replies": [{}, {}]}])
    request = {
        "operation": "add-text-box",
        "deck_id": "explicit",
        "body": {
            "slide_id": "slide_one",
            "text": "Hello",
            "x": 10,
            "y": 20,
            "width": 120,
            "height": 40,
        },
    }
    result = workflow.run(request, "synthetic", lambda *_: api)
    payload = api.calls[-1][2]
    assert payload["writeControl"] == {"requiredRevisionId": "revision-1"}
    assert (
        payload["requests"][0]["createShape"]["objectId"]
        == payload["requests"][1]["insertText"]["objectId"]
    )
    assert "incomplete" in result["review"]


@pytest.mark.parametrize(
    "requests",
    [
        [{"deleteObject": {"objectId": "missing"}}],
        [{"permissions": {}}],
        [{"insertText": {"objectId": "slide_one", "text": "bad"}}],
        [
            {
                "createImage": {
                    "objectId": "new_image",
                    "url": "https://127.0.0.1/image.png",
                    "elementProperties": {"pageObjectId": "slide_one"},
                }
            }
        ],
        [
            {"deleteObject": {"objectId": "shape_one"}},
            {"insertText": {"objectId": "shape_one", "text": "stale"}},
        ],
    ],
)
def test_raw_batch_refusals(workflow, deck, requests):
    with pytest.raises(workflow.Failure):
        workflow.validate_batch(requests, copy.deepcopy(deck))


def test_duplicate_slide_maps_all_ids_and_can_be_edited(workflow, deck):
    requests = workflow.operations("add-slide", {"duplicate_slide_id": "slide_one"}, deck)
    mapping = requests[0]["duplicateObject"]["objectIds"]
    requests.append({"insertText": {"objectId": mapping["shape_one"], "text": "new"}})
    assert workflow.validate_batch(requests, deck) == requests
    assert set(mapping) == {"slide_one", "shape_one"}


def test_render_download_and_save_are_required_before_review(workflow, deck, fake_api):
    api = fake_api([deck])
    downloaded = []

    def download(client, deck_id, slide_id):
        downloaded.append((deck_id, slide_id))
        return b"fixture-png"

    result = workflow.run(
        {"operation": "render", "deck_id": "explicit", "body": {"slide_ids": ["slide_one"]}},
        "synthetic",
        lambda *_: api,
        download,
        lambda data: "/mnt/session/outputs/fixture.png",
    )
    assert downloaded == [("explicit", "slide_one")]
    assert result["files"] == ["/mnt/session/outputs/fixture.png"]
    assert "visually inspect" in result["review"]


def test_render_failure_retains_target_and_never_claims_done(workflow, deck, fake_api):
    api = fake_api([deck])

    def fail(*args):
        raise workflow.Failure("render failed")

    with pytest.raises(workflow.Failure) as error:
        workflow.run(
            {"operation": "render", "deck_id": "explicit", "body": {"slide_ids": ["slide_one"]}},
            "synthetic",
            lambda *_: api,
            fail,
        )
    assert error.value.details["deck_id"] == "explicit"
    assert "incomplete" in error.value.details["review"]


def test_no_implicit_deck_and_no_unknown_mutation(workflow, fake_api, deck):
    api = fake_api([deck])
    with pytest.raises(workflow.Failure, match="resource ID"):
        workflow.run(
            {"operation": "delete", "body": {"object_ids": ["slide_one"]}},
            "synthetic",
            lambda *_: api,
        )
    assert not api.calls


def test_copy_without_folder_uses_shared_accounts_root_not_template_folder(workflow, fake_api):
    api = fake_api([{"id": "shared_root"}, {"id": "new_copy"}])
    result = workflow.run(
        {"operation": "copy-template", "body": {"template_id": "template", "title": "Disposable"}},
        "synthetic",
        lambda *_: api,
    )
    assert api.calls[0] == ("GET", "/drive/v3/files/root?fields=id&supportsAllDrives=true", None)
    assert api.calls[1][2]["parents"] == ["shared_root"]
    assert result["deck_id"] == "new_copy"


@pytest.mark.parametrize("elements", [[], [{"paragraphMarker": {}}]])
def test_set_text_fills_empty_placeholder_without_delete(workflow, deck, elements):
    deck["slides"][0]["pageElements"][0]["shape"]["text"] = {"textElements": elements}
    requests = workflow.operations("set-text", {"texts": {"shape_one": "Filled"}}, deck)
    assert requests == [
        {"insertText": {"objectId": "shape_one", "insertionIndex": 0, "text": "Filled"}}
    ]


def test_set_text_replaces_existing_grouped_shape_text(workflow, deck):
    deck["slides"][0]["pageElements"] = [
        {
            "objectId": "group",
            "elementGroup": {
                "children": [
                    {
                        "objectId": "nested",
                        "shape": {"text": {"textElements": [{"textRun": {"content": "Old\n"}}]}},
                    }
                ]
            },
        }
    ]
    requests = workflow.operations("set-text", {"texts": {"nested": "New"}}, deck)
    assert requests[0] == {"deleteText": {"objectId": "nested", "textRange": {"type": "ALL"}}}
    assert requests[1]["insertText"]["text"] == "New"


def test_clearing_already_empty_shape_does_not_send_batch(workflow, deck, fake_api):
    api = fake_api([deck])
    result = workflow.run(
        {"operation": "set-text", "deck_id": "explicit", "body": {"texts": {"shape_one": ""}}},
        "synthetic",
        lambda *_: api,
    )
    assert len(api.calls) == 1 and "no changes" in result["review"]


@pytest.mark.parametrize("content", ["\n", " \n", "\t\n", "\n\n"])
def test_set_text_deletes_whitespace_only_content_before_inserting(workflow, deck, content):
    deck["slides"][0]["pageElements"][0]["shape"]["text"] = {
        "textElements": [{"textRun": {"content": content}}]
    }
    requests = workflow.operations("set-text", {"texts": {"shape_one": "Filled"}}, deck)
    assert requests == [
        {"deleteText": {"objectId": "shape_one", "textRange": {"type": "ALL"}}},
        {"insertText": {"objectId": "shape_one", "insertionIndex": 0, "text": "Filled"}},
    ]
