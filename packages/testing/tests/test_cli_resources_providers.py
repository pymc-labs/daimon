"""Provider-host cases of test_cli_resources: they need the OpenAI/Gemini host modules,
so they land with the provider runtimes.
"""

from __future__ import annotations

from pathlib import Path

from daimon.testing.cli_resources import (
    OPT_IN_OPERATOR_PROFILE,
    CliResourceEdge,
    cli_check_holds,
    opt_in_operator_profile,
)

from .test_cli_resources import CHANNEL as CHANNEL
from .test_cli_resources import DATA as DATA
from .test_cli_resources import NATIVE as NATIVE
from .test_cli_resources import WORKSPACE as WORKSPACE
from .test_cli_resources import _OpenAIServer as _OpenAIServer
from .test_cli_resources import _Run as _Run
from .test_cli_resources import _scenario as _scenario
from .test_cli_resources import run as run


async def test_d14_openai_under_the_opt_in_profile(run: _Run, tmp_path: Path) -> None:
    scenario = _scenario("QA-D14-FORK-KEEPS-SKILLS")
    # The base agent's own skill, through N6's resource edge (as the column's setup does).
    added = await run.binding.execute(
        {
            "do": "admin",
            "tool": "cli",
            "args": "daimon skills --guild {guild_id} add fixtures/SKILL.md --agent daimon --yes",
        },
        run.values(),
    )
    assert added.status == "executed"
    assert run.resources.host is not None
    base = await run.resources.host.find("daimon")
    assert base is not None and base.revision.agent.spec.skills

    async with opt_in_operator_profile(
        run.binding,
        run.driver,
        workspace="project",
        root=tmp_path,
        fallback=run.resources,
    ) as edge:
        fork = await run.binding.execute(scenario["steps"][1], run.values())
        # Real CLI execution, under the opt-in profile.
        assert fork.status == "executed"
        assert fork.observations["profile"] == OPT_IN_OPERATOR_PROFILE
        assert "a copy of daimon" in str(fork.observations["output"])
        name = fork.values["fork.name"]

        # Independent readback: the copy is in the catalog, with the base's skill copied.
        copy = await run.resources.host.find(name)
        assert copy is not None and copy.revision.agent.spec.skills
        assert copy.revision.agent.spec.model.id == "gpt-6-luna"

        # The frozen cli_check assertions, through the real `daimon agents … get`.
        values: dict[str, object] = {
            "env.qa_base_with_own_skill.skills": ["qa-echo-skill"],
            "env.qa_base_with_own_skill.model": "gpt-6-luna",
        }
        verdicts: list[bool] = []
        for assertion in scenario["assert"][:2]:
            command = assertion["cmd"].replace("{guild_id}", WORKSPACE).replace("{fork.name}", name)
            receipt = await edge.cli_check(command)
            assert receipt.error is None and copy.id in receipt.output, "read back by the CLI"
            verdicts.append(cli_check_holds(assertion, receipt, values))
        # Under the opt-in profile the CLI's table readback (name, id, description,
        # created_at, as on Anthropic) shows neither the skill nor the model: both
        # frozen checks FAIL, although the catalog readback above holds both.
        assert verdicts == [False, False]

        teardown = await run.binding.execute(scenario["teardown"][0], run.values())
    # The native copy's archive is N6's typed pending edge: the cell cannot complete.
    assert (teardown.status, teardown.code) == ("pending", "ADMIN_NEUTRAL_COPY_ARCHIVE_UNBOUND")
    assert isinstance(edge, CliResourceEdge) and len(edge.checks) == 2
