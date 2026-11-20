"""GitHub on `api.github.com`: REST and GraphQL routed to effects, issues kept in the overlay,
simulated answers checked against GitHub's published descriptions.

`github_rest.json` (response schemas of the routed operations) and `github.graphql` (the public
schema, descriptions dropped) are vendored by `scripts/vendor_github_api.py`. GraphQL answers
are checked only where the `evals` extra (graphql-core) is installed. Resource keys:
repo `owner/repo`, issue and pull `owner/repo#n`, content `owner/repo:path`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import urlsplit

import jsonschema
from daimon.core.evals.format import Body, Effect, ExchangeResponse, HttpResponse, JsonObject
from daimon.core.evals.world.model import HttpCall, ItemRef, WorldRequest, body_json
from daimon.core.evals.world.overlay import Overlay
from pydantic import JsonValue

if TYPE_CHECKING:
    from graphql import GraphQLResolveInfo, GraphQLSchema

HOSTS: Final = frozenset({"api.github.com"})
_HERE: Final = Path(__file__).parent


@dataclass(frozen=True)
class Route:
    method: str
    template: str
    verb: str | None
    resource: str | None = None  # the resource type the path names

    @property
    def operation(self) -> str:
        return f"{self.method} {self.template}"

    @property
    def pattern(self) -> re.Pattern[str]:
        return _compile(self.template)


@cache
def _compile(template: str) -> re.Pattern[str]:
    body = re.sub(
        r"\{(\w+)\}",
        lambda m: f"(?P<{m[1]}>.+)" if m[1] == "path" else f"(?P<{m[1]}>[^/]+)",
        template,
    )
    return re.compile(f"^{body}/?$")


_R: Final = "/repos/{owner}/{repo}"
ROUTES: Final = (
    Route("GET", "/user", None),
    Route("GET", _R, "github.repo.read", "repo"),
    Route("GET", f"{_R}/issues", "github.issue.list"),
    Route("POST", f"{_R}/issues", "github.issue.create"),
    Route("GET", f"{_R}/issues/{{issue_number}}", "github.issue.read", "issue"),
    Route("PATCH", f"{_R}/issues/{{issue_number}}", "github.issue.update", "issue"),
    Route("GET", f"{_R}/issues/{{issue_number}}/comments", "github.issue.read"),
    Route("POST", f"{_R}/issues/{{issue_number}}/comments", "github.issue.comment.create"),
    Route("GET", f"{_R}/pulls", "github.pull.list"),
    Route("POST", f"{_R}/pulls", "github.pull.create"),
    Route("GET", f"{_R}/pulls/{{pull_number}}", "github.pull.read", "pull"),
    Route("GET", f"{_R}/pulls/{{pull_number}}/files", "github.pull.read"),
    Route("POST", f"{_R}/pulls/{{pull_number}}/reviews", "github.pull.review.create"),
    Route("PUT", f"{_R}/pulls/{{pull_number}}/merge", "github.pull.merge"),
    Route("GET", f"{_R}/contents/{{path}}", "github.content.read", "content"),
    Route("PUT", f"{_R}/contents/{{path}}", "github.content.write"),
    Route("POST", f"{_R}/actions/workflows/{{workflow_id}}/dispatches", "github.workflow.dispatch"),
    Route("POST", f"{_R}/releases", "github.release.create"),
    Route("GET", "/search/issues", "github.issue.list"),
)
#: GraphQL mutation fields with an effect; queries have none beyond reading.
_MUTATIONS: Final = {
    "createIssue": "github.issue.create",
    "updateIssue": "github.issue.update",
    "addComment": "github.issue.comment.create",
    "createPullRequest": "github.pull.create",
    "addPullRequestReview": "github.pull.review.create",
    "mergePullRequest": "github.pull.merge",
    "createCommitOnBranch": "github.content.write",
}


def route_of(method: str, path: str) -> tuple[Route, dict[str, str]] | None:
    for route in ROUTES:
        if route.method == method.upper() and (found := route.pattern.match(path)):
            return route, found.groupdict()
    return None


def _key(route: Route, params: dict[str, str]) -> str | None:
    repo = f"{params.get('owner')}/{params.get('repo')}"
    match route.resource:
        case "repo":
            return repo
        case "issue":
            return f"{repo}#{params['issue_number']}"
        case "pull":
            return f"{repo}#{params['pull_number']}"
        case "content":
            return f"{repo}:{params['path']}"
        case _:
            return None


@cache
def _rest() -> JsonObject:
    loaded: JsonObject = json.loads((_HERE / "github_rest.json").read_text())
    return loaded


@cache
def graphql_schema() -> GraphQLSchema:
    from graphql import build_schema

    return build_schema((_HERE / "github.graphql").read_text())


def _by_response_key(source: object, info: GraphQLResolveInfo[object], **_args: object) -> object:
    """Read the answer under the field's response key (its alias, else its name)."""
    return cast("JsonObject", source).get(str(info.path.key)) if isinstance(source, dict) else None


def graphql_problem(query: str, variables: JsonValue, answer: JsonValue) -> str | None:
    """Why `answer` cannot be GitHub's reply to `query`: the query against the schema, then
    the answer's data run through the query (types, nullability, enum values)."""
    if not isinstance(answer, dict):
        return "A GraphQL answer is a JSON object."
    try:
        from graphql import GraphQLError, parse, validate
        from graphql.execution import execute_sync  # pyright: ignore[reportUnknownVariableType]
    except ImportError:
        return None
    try:
        document = parse(query)
    except GraphQLError as exc:
        return f"The query does not parse: {exc.message}"
    schema = graphql_schema()
    if errors := validate(schema, document):
        return errors[0].message if "errors" not in answer else None
    data = answer.get("data")
    if data is None:
        return None if answer.get("errors") else "A GraphQL answer has data or errors."
    result = execute_sync(
        schema, document, root_value=data, field_resolver=_by_response_key,
        variable_values=variables if isinstance(variables, dict) else None,
    )  # fmt: skip
    return f"The data does not fit the query: {result.errors[0].message}" if result.errors else None


def rest_problem(operation: str, status: int, body: JsonValue) -> str | None:
    """Why `body` is not a valid `status` response of `operation`, per GitHub's description."""
    schemas = _rest()["operations"]
    assert isinstance(schemas, dict)
    operation_schemas = schemas.get(operation)
    if not isinstance(operation_schemas, dict):
        return None
    schema = operation_schemas.get(str(status))
    if schema is None:
        known = sorted(operation_schemas)
        return f"{operation} answers {', '.join(known)}, not {status}." if known else None
    validator = jsonschema.Draft202012Validator(
        {**cast("dict[str, JsonValue]", schema), "$defs": _rest()["$defs"]}
    )
    error = next(iter(validator.iter_errors(body)), None)
    if error is None:
        return None
    return f"At {error.json_path}: {error.message[:300]}"


def _number(repo: str, overlay: Overlay) -> int:
    """The next issue or pull number in `repo`: past every known one, else a stable start."""
    known = [
        int(key.rsplit("#", 1)[1])
        for kind in ("issue", "pull")
        for key, _ in overlay.list("github", kind)
        if key.startswith(f"{repo}#") and key.rsplit("#", 1)[1].isdigit()
    ]
    if known:
        return max(known) + 1
    return 100 + int(hashlib.sha256(repo.encode()).hexdigest()[:4], 16) % 900


def _json(status: int, data: JsonValue) -> ExchangeResponse:
    return ExchangeResponse(
        http=HttpResponse(
            status=status,
            headers={"content-type": "application/json; charset=utf-8"},
            body=Body(json=data, media_type="application/json"),
        )
    )


class GithubService:
    name = "github"

    def claims(self, item: ItemRef, call: HttpCall) -> bool:
        return call.host in HOSTS

    @staticmethod
    def _split(request: WorldRequest) -> tuple[str, str]:
        http = request.request.http
        assert http is not None
        return http.method.upper(), urlsplit(http.url).path

    def effects(self, request: WorldRequest) -> tuple[Effect, ...]:
        if request.surface != "http":
            return ()
        method, path = self._split(request)
        if path == "/graphql":
            query = body_json(request.request.http.body if request.request.http else None)
            text = query.get("query") if isinstance(query, dict) else None
            if not isinstance(text, str) or not text.lstrip().startswith("mutation"):
                return ()
            return tuple(Effect(verb=v) for name, v in _MUTATIONS.items() if f"{name}(" in text)
        found = route_of(method, path)
        if found is None or found[0].verb is None:
            return ()
        params = found[1]
        number = params.get("issue_number") or params.get("pull_number")
        target = f"github:{params['owner']}/{params['repo']}" if "owner" in params else None
        target = f"{target}#{number}" if target and number else target
        args = body_json(request.request.http.body if request.request.http else None)
        fields = args if isinstance(args, dict) else None
        return (Effect(verb=found[0].verb, target=target, args=fields),)

    def resource(self, request: WorldRequest) -> tuple[str, str] | None:
        if request.surface != "http":
            return None
        found = route_of(*self._split(request))
        key = _key(*found) if found else None
        return (found[0].resource, key) if found and found[0].resource and key else None

    def _identity(self, item: ItemRef) -> JsonObject:
        who = next((i for i in item.case.world.identities or () if i.service == "github"), None)
        login = who.login if who else "octocat"
        user_id = int(hashlib.sha256(login.encode()).hexdigest()[:7], 16)
        return {
            "login": login, "id": user_id, "type": "User",
            "name": who.display_name if who else None, "email": who.email if who else None,
            "html_url": f"https://github.com/{login}", "site_admin": False,
        }  # fmt: skip

    def answer(
        self, item: ItemRef, request: WorldRequest, overlay: Overlay
    ) -> ExchangeResponse | None:
        if request.surface != "http":
            return None
        method, path = self._split(request)
        found = route_of(method, path)
        if found is None:
            return None
        route, params = found
        if route.template == "/user":
            return _json(200, self._identity(item))
        key = _key(route, params)
        body = body_json(request.request.http.body if request.request.http else None)
        fields: JsonObject = body if isinstance(body, dict) else {}
        repo = f"{params.get('owner')}/{params.get('repo')}"
        if method == "GET" and key is not None and route.resource is not None:
            known = overlay.get("github", route.resource, key)
            return _json(200, known) if known is not None else None
        if route.verb == "github.issue.create":
            number = _number(repo, overlay)
            issue: JsonObject = {
                "number": number, "title": fields.get("title"), "body": fields.get("body"),
                "state": "open", "labels": [{"name": n} for n in _names(fields.get("labels"))],
                "assignees": [{"login": n} for n in _names(fields.get("assignees"))],
                "comments": 0, "user": self._identity(item),
                "url": f"https://api.github.com/repos/{repo}/issues/{number}",
                "html_url": f"https://github.com/{repo}/issues/{number}",
                "created_at": _stamp(item), "updated_at": _stamp(item),
            }  # fmt: skip
            overlay.put("github", "issue", f"{repo}#{number}", issue)
            return _json(201, issue)
        if route.verb == "github.issue.update" and key is not None:
            known = overlay.get("github", "issue", key)
            if known is None:
                return None
            updated = known | {k: v for k, v in fields.items() if k in ("title", "body", "state")}
            if "labels" in fields:
                updated["labels"] = [{"name": n} for n in _names(fields["labels"])]
            overlay.put("github", "issue", key, updated)
            return _json(200, updated)
        if route.verb == "github.issue.comment.create":
            issue_key = f"{repo}#{params['issue_number']}"
            known = overlay.get("github", "issue", issue_key)
            count = len(overlay.list("github", "comment")) + 1
            comment_id = int(hashlib.sha256(f"{issue_key}/{count}".encode()).hexdigest()[:9], 16)
            comment: JsonObject = {
                "id": comment_id, "body": fields.get("body"), "user": self._identity(item),
                "html_url": f"https://github.com/{repo}/issues/{params['issue_number']}"
                f"#issuecomment-{comment_id}",
                "created_at": _stamp(item), "updated_at": _stamp(item),
            }  # fmt: skip
            overlay.put("github", "comment", f"{issue_key}/{comment_id}", comment)
            if known is not None:
                comments = known.get("comments")
                overlay.put(
                    "github", "issue", issue_key,
                    known | {"comments": (comments if isinstance(comments, int) else 0) + 1},
                )  # fmt: skip
            return _json(201, comment)
        if route.verb == "github.workflow.dispatch":
            return ExchangeResponse(http=HttpResponse(status=204))
        return None

    def remember(self, request: WorldRequest, response: ExchangeResponse, overlay: Overlay) -> None:
        http, answer = request.request.http, response.http
        if request.surface != "http" or http is None or answer is None:
            return
        found = route_of(http.method, urlsplit(http.url).path)
        data = body_json(answer.body)
        if http.method.upper() == "GET" or found is None or not isinstance(data, dict):
            return  # reads change nothing
        if not 200 <= answer.status < 300:
            return
        route, params = found
        repo = f"{params.get('owner')}/{params.get('repo')}"
        number = data.get("number")
        if route.verb in ("github.issue.create", "github.pull.create") and isinstance(number, int):
            kind = "issue" if route.verb == "github.issue.create" else "pull"
            overlay.put("github", kind, f"{repo}#{number}", data)
        elif route.resource is not None and (key := _key(route, params)) is not None:
            overlay.put("github", route.resource, key, data)

    def check(self, request: WorldRequest, response: ExchangeResponse) -> str | None:
        http, answer = request.request.http, response.http
        if http is None or answer is None:
            return "GitHub answers HTTP."
        method, path = self._split(request)
        body = body_json(answer.body)
        if answer.body is not None and body is None and answer.status != 204:
            return "GitHub's API answers JSON."
        if path == "/graphql":
            asked = body_json(http.body)
            query = asked.get("query") if isinstance(asked, dict) else None
            if not isinstance(query, str):
                return None
            assert isinstance(asked, dict)
            return graphql_problem(query, asked.get("variables"), body)
        found = route_of(method, path)
        if found is None or not 200 <= answer.status < 300:
            return None
        return rest_problem(found[0].operation, answer.status, body)

    def guidance(self, item: ItemRef, request: WorldRequest) -> str:
        if request.surface != "http":
            return ""
        method, path = self._split(request)
        if path == "/graphql":
            return 'GitHub GraphQL API v4: answer {"data": ...} shaped exactly by the query.'
        found = route_of(method, path)
        name = found[0].operation if found else f"{method} {path}"
        return f"GitHub REST API v3, operation {name}. Use GitHub's exact JSON field names."


def _names(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        return []
    named = (v for v in value if isinstance(v, str | dict))
    return [v if isinstance(v, str) else str(v.get("name")) for v in named]


def _stamp(item: ItemRef) -> str:
    now: datetime = item.case.clock.now
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")
