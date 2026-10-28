"""Template-led Google Slides edits, with mandatory rendered-review evidence."""

from __future__ import annotations

import ipaddress
import uuid
from urllib.parse import urlsplit

from workflow_io import Api, Failure, fetch, fields, identifier, main, require, save_png, text

# Supported API request schema, not an operator-configured resource allowlist.
STYLING = {
    "updateTextStyle",
    "updateParagraphStyle",
    "createParagraphBullets",
    "deleteParagraphBullets",
    "updateShapeProperties",
    "updatePageProperties",
    "updatePageElementTransform",
    "updateImageProperties",
    "updateLineProperties",
    "updatePageElementsZOrder",
    "updateSlidesPosition",
}
SUPPORTED = STYLING | {
    "createSlide",
    "duplicateObject",
    "createShape",
    "createImage",
    "insertText",
    "deleteText",
    "replaceAllText",
    "deleteObject",
}


def new_id():
    return "wf_" + uuid.uuid4().hex[:16]


def public_https(value):
    require(isinstance(value, str), "Expected an image URL")
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    require(
        parsed.scheme == "https"
        and host
        and not parsed.username
        and not parsed.password
        and parsed.port in (None, 443),
        "Image URL must be public HTTPS without credentials",
    )
    require(
        "." in host and not host.endswith((".local", ".localhost", ".internal")),
        "Image URL must name a public host",
    )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        require(address.is_global, "Image URL cannot address a private network")
    return value


def inspect_deck(deck):
    slides, objects, shapes = [], {}, set()

    def walk(element, page):
        object_id = identifier(element["objectId"])
        objects[object_id] = page
        if "shape" in element:
            shapes.add(object_id)
        for child in element.get("elementGroup", {}).get("children", []):
            walk(child, page)

    for slide in deck.get("slides", []):
        page = identifier(slide["objectId"])
        slides.append(page)
        objects[page] = page
        for element in slide.get("pageElements", []):
            walk(element, page)
    require(slides, "Deck has no slides")
    return slides, objects, shapes


def validate_batch(requests, deck):
    require(isinstance(requests, list) and 0 < len(requests) <= 1000, "Expected 1–1000 requests")
    slides, objects, shapes = inspect_deck(deck)
    for request in requests:
        require(isinstance(request, dict) and len(request) == 1, "One operation per batch request")
        kind, body = next(iter(request.items()))
        require(kind in SUPPORTED and isinstance(body, dict), "Unsupported Slides request")
        if kind == "deleteObject":
            fields(body, ("objectId",), ("objectId",))
            target = identifier(body["objectId"])
            require(target in objects, "Unknown deletion target")
            if target in slides:
                require(len(slides) > 1, "Cannot delete final slide")
                slides.remove(target)
                for item in list(objects):
                    if objects[item] == target:
                        del objects[item]
                        shapes.discard(item)
            else:
                # Group removal can delete children too. Reject deleting groups in a raw
                # batch so subsequent operations cannot target a stale child.
                require(
                    target in shapes
                    or any(
                        target == e.get("objectId") and "elementGroup" not in e
                        for page in deck["slides"]
                        for e in page.get("pageElements", [])
                    ),
                    "Delete group children explicitly",
                )
                del objects[target]
                shapes.discard(target)
        elif kind == "createSlide":
            target = identifier(body.get("objectId"))
            require(target not in objects, "Duplicate object ID")
            slides.append(target)
            objects[target] = target
        elif kind == "duplicateObject":
            source = body.get("objectId")
            require(source in slides, "Duplicate an existing slide explicitly")
            fields(body, ("objectId", "objectIds"), ("objectId", "objectIds"))
            mapping = body["objectIds"]
            children = [item for item, page in objects.items() if page == source]
            require(
                isinstance(mapping, dict) and set(mapping) == set(children),
                "Provide new IDs for the slide and every copied element",
            )
            new_values = [identifier(value) for value in mapping.values()]
            require(
                len(set(new_values)) == len(new_values) and not (set(new_values) & set(objects)),
                "Duplicate object ID",
            )
            page = mapping[source]
            slides.append(page)
            for item in children:
                objects[mapping[item]] = page
                if item in shapes:
                    shapes.add(mapping[item])
        elif kind in ("createShape", "createImage"):
            target = identifier(body.get("objectId"))
            require(target not in objects, "Duplicate object ID")
            page = body.get("elementProperties", {}).get("pageObjectId")
            require(page in slides, "Unknown slide for new element")
            objects[target] = page
            if kind == "createShape":
                shapes.add(target)
            else:
                public_https(body.get("url"))
        elif kind in ("insertText", "deleteText"):
            require(body.get("objectId") in shapes, "Text target is not a shape in this deck")
        elif kind == "replaceAllText":
            require(text(body.get("containsText", {}).get("text")), "Empty replacement search")
            require(isinstance(body.get("replaceText"), str), "Expected replacement text")
            require(set(body.get("pageObjectIds", [])) <= set(slides), "Unknown replacement slide")
        elif kind == "updateSlidesPosition":
            require(
                body.get("slideObjectIds") and set(body["slideObjectIds"]) <= set(slides),
                "Unknown slide",
            )
        elif kind == "updatePageElementsZOrder":
            require(
                body.get("pageElementObjectIds")
                and set(body["pageElementObjectIds"]) <= set(objects),
                "Unknown element",
            )
        else:
            target = body.get("objectId") or body.get("pageObjectId")
            require(target in objects, "Unknown style target")

            # Background images are also fetched by Google; validate every URL in style JSON.
            def urls(value):
                if isinstance(value, dict):
                    for name, item in value.items():
                        if name.lower().endswith("url"):
                            public_https(item)
                        else:
                            urls(item)
                elif isinstance(value, list):
                    for item in value:
                        urls(item)

            urls(body)
    return requests


def box(body):
    for name in ("x", "y", "width", "height"):
        require(
            type(body.get(name)) in (int, float) and body[name] >= 0,
            "Expected nonnegative point geometry",
        )
    require(body["width"] > 0 and body["height"] > 0, "Box must have positive size")
    return {
        "pageObjectId": identifier(body["slide_id"]),
        "size": {
            "width": {"magnitude": body["width"], "unit": "PT"},
            "height": {"magnitude": body["height"], "unit": "PT"},
        },
        "transform": {
            "scaleX": 1,
            "scaleY": 1,
            "translateX": body["x"],
            "translateY": body["y"],
            "unit": "PT",
        },
    }


def shape_texts(deck):
    result = {}

    def walk(element):
        if "shape" in element:
            result[element["objectId"]] = "".join(
                item.get("textRun", {}).get("content", "")
                for item in element["shape"].get("text", {}).get("textElements", [])
            ).rstrip("\n")
        for child in element.get("elementGroup", {}).get("children", []):
            walk(child)

    for slide in deck.get("slides", []):
        for element in slide.get("pageElements", []):
            walk(element)
    return result


def operations(operation, body, deck):
    if operation == "batch":
        fields(body, ("requests",), ("requests",))
        return body["requests"]
    if operation == "set-text":
        fields(body, ("texts",), ("texts",))
        require(
            isinstance(body["texts"], dict) and body["texts"], "Expected shape ID to text mapping"
        )
        _, _, shapes = inspect_deck(deck)
        existing_text = shape_texts(deck)
        result = []
        for target, value in body["texts"].items():
            require(
                target in shapes and isinstance(value, str), "Unknown text shape or non-string text"
            )
            if existing_text.get(target):
                result.append({"deleteText": {"objectId": target, "textRange": {"type": "ALL"}}})
            if value:
                result.append(
                    {"insertText": {"objectId": target, "insertionIndex": 0, "text": value}}
                )
        return result
    if operation == "replace-text":
        fields(body, ("replacements", "slide_ids"), ("replacements",))
        require(
            isinstance(body["replacements"], dict) and body["replacements"], "Expected replacements"
        )
        return [
            {
                "replaceAllText": {
                    "containsText": {"text": text(find), "matchCase": True},
                    "replaceText": value,
                    **({"pageObjectIds": body["slide_ids"]} if "slide_ids" in body else {}),
                }
            }
            for find, value in body["replacements"].items()
        ]
    if operation == "delete":
        fields(body, ("object_ids",), ("object_ids",))
        require(
            isinstance(body["object_ids"], list) and body["object_ids"], "Name deletion targets"
        )
        return [
            {"deleteObject": {"objectId": identifier(target)}}
            for target in dict.fromkeys(body["object_ids"])
        ]
    if operation == "add-slide":
        fields(body, ("duplicate_slide_id",))
        if "duplicate_slide_id" not in body:
            return [
                {
                    "createSlide": {
                        "objectId": new_id(),
                        "slideLayoutReference": {"predefinedLayout": "BLANK"},
                    }
                }
            ]
        source = identifier(body["duplicate_slide_id"])
        _, objects, _ = inspect_deck(deck)
        mapping = {item: new_id() for item, page in objects.items() if page == source}
        return [{"duplicateObject": {"objectId": source, "objectIds": mapping}}]
    if operation in ("add-image", "add-text-box"):
        fields(
            body,
            (
                "slide_id",
                "x",
                "y",
                "width",
                "height",
                "url" if operation == "add-image" else "text",
            ),
            (
                "slide_id",
                "x",
                "y",
                "width",
                "height",
                "url" if operation == "add-image" else "text",
            ),
        )
        target = new_id()
        properties = box(body)
        if operation == "add-image":
            return [
                {
                    "createImage": {
                        "objectId": target,
                        "url": public_https(body["url"]),
                        "elementProperties": properties,
                    }
                }
            ]
        value = text(body["text"])
        return [
            {
                "createShape": {
                    "objectId": target,
                    "shapeType": "TEXT_BOX",
                    "elementProperties": properties,
                }
            },
            {"insertText": {"objectId": target, "text": value, "insertionIndex": 0}},
        ]
    raise Failure("Unsupported Slides operation")


def thumbnail(api, deck_id, slide_id):
    result = api.request(
        "GET",
        f"/v1/presentations/{deck_id}/pages/{slide_id}/thumbnail?thumbnailProperties.mimeType=PNG&thumbnailProperties.thumbnailSize=LARGE",
    )
    url = result["contentUrl"]
    host = urlsplit(public_https(url)).hostname
    require(host.endswith(".googleusercontent.com"), "Unexpected Google render host")
    return fetch("GET", url, {})  # Signed thumbnail URL: never forward Authorization.


def run(request, token, api_factory=Api, download=thumbnail, save=save_png):
    fields(request, ("operation", "deck_id", "body"), ("operation", "body"))
    operation, body = request["operation"], request["body"]
    api = api_factory("https://slides.googleapis.com", "Bearer " + token)
    if operation == "copy-template":
        require("deck_id" not in request, "Copy names a template, not a target deck")
        fields(body, ("template_id", "title", "folder_id"), ("template_id", "title"))
        source, title = identifier(body["template_id"]), text(body["title"])
        drive = api_factory("https://www.googleapis.com", "Bearer " + token)
        # Explicitly select My Drive rather than inheriting a template folder.
        folder = body.get("folder_id")
        if folder is None:
            folder = drive.request("GET", "/drive/v3/files/root?fields=id&supportsAllDrives=true")[
                "id"
            ]
        payload = {"name": title, "parents": [identifier(folder)]}
        result = drive.request(
            "POST", f"/drive/v3/files/{source}/copy?supportsAllDrives=true", payload
        )
        return {
            "deck_id": result["id"],
            "review": "incomplete: edit and render before reporting done",
        }
    deck_id = identifier(request.get("deck_id"))
    try:
        deck = api.request("GET", f"/v1/presentations/{deck_id}")
        slides, _, _ = inspect_deck(deck)
        if operation == "read":
            fields(body, ())
            return {"deck_id": deck_id, "slides": deck["slides"], "page_size": deck.get("pageSize")}
        if operation == "render":
            fields(body, ("slide_ids",), ("slide_ids",))
            require(
                isinstance(body["slide_ids"], list)
                and body["slide_ids"]
                and set(body["slide_ids"]) <= set(slides),
                "Name existing slides to render",
            )
            files = [save(download(api, deck_id, slide)) for slide in body["slide_ids"]]
            return {
                "deck_id": deck_id,
                "files": files,
                "review": "rendered; visually inspect PNGs before reporting done",
            }
        planned = operations(operation, body, deck)
        if operation == "set-text" and not planned:
            return {"deck_id": deck_id, "review": "no changes; target shapes were already empty"}
        requests = validate_batch(planned, deck)
        revision = deck.get("revisionId")
        require(revision, "Missing revision ID; refresh deck before writing")
        result = api.request(
            "POST",
            f"/v1/presentations/{deck_id}:batchUpdate",
            {"requests": requests, "writeControl": {"requiredRevisionId": revision}},
        )
        return {
            "deck_id": deck_id,
            "replies": result.get("replies", []),
            "review": "edited; review incomplete: render changed slides and inspect PNGs",
        }
    except Failure as exc:
        raise Failure(
            str(exc), deck_id=deck_id, review="incomplete; reconcile deck before retrying"
        ) from None


if __name__ == "__main__":
    raise SystemExit(main(run, "google"))
