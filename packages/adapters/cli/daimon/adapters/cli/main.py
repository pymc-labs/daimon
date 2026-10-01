"""Typer root + sub-app wiring."""

from __future__ import annotations

import importlib.metadata
import io
import sys
import traceback

import click
import typer
from daimon.adapters.cli.commands.agents import agents_app
from daimon.adapters.cli.commands.audit import audit_app
from daimon.adapters.cli.commands.backup import backup_app
from daimon.adapters.cli.commands.channels import channels_app
from daimon.adapters.cli.commands.config import config_app
from daimon.adapters.cli.commands.crypto import crypto_app
from daimon.adapters.cli.commands.defaults import defaults_app
from daimon.adapters.cli.commands.environments import environments_app
from daimon.adapters.cli.commands.help import help_app
from daimon.adapters.cli.commands.mcp import mcp_app
from daimon.adapters.cli.commands.memory import memory_app
from daimon.adapters.cli.commands.notebook import notebook_app
from daimon.adapters.cli.commands.promo import promo_app
from daimon.adapters.cli.commands.repo_bindings import repo_bindings_app
from daimon.adapters.cli.commands.routines import routines_app
from daimon.adapters.cli.commands.sessions import sessions_app
from daimon.adapters.cli.commands.skills import skills_app
from daimon.adapters.cli.commands.smoke import smoke_command
from daimon.adapters.cli.commands.tenants import tenants_app
from daimon.adapters.cli.commands.usage import usage_app
from daimon.adapters.cli.logging import configure_bootstrap_logging
from daimon.adapters.cli.run.command import run_command
from daimon.core.observability import install_log_redaction, redact_text

# Typer's pretty exceptions print frame locals and raw messages; unhandled
# errors go through `main` instead, which prints a redacted traceback.
app = typer.Typer(help="Daimon CMA CLI", pretty_exceptions_enable=False)
app.add_typer(agents_app, name="agents")
app.add_typer(backup_app, name="backup")
app.add_typer(channels_app, name="channels")
app.add_typer(environments_app, name="environments")
app.add_typer(tenants_app, name="tenants")
app.add_typer(usage_app, name="usage")
app.add_typer(audit_app, name="audit")
app.add_typer(sessions_app, name="sessions")
app.add_typer(config_app, name="config")
app.add_typer(crypto_app, name="crypto")
app.add_typer(defaults_app, name="defaults")
app.add_typer(skills_app, name="skills")
app.add_typer(help_app, name="help")
app.add_typer(mcp_app, name="mcp")
app.add_typer(memory_app, name="memory")
app.add_typer(notebook_app, name="notebook")
app.add_typer(promo_app, name="promo")
app.add_typer(repo_bindings_app, name="repo-bindings")
app.add_typer(routines_app, name="routines")
app.command("run")(run_command)
app.command("smoke")(smoke_command)


@app.callback()
def root() -> None:
    # Every command logs through the redacting bootstrap chain (no frame
    # locals); unhandled exceptions are rendered by `main`, redacted.
    configure_bootstrap_logging()
    install_log_redaction()


@app.command("version")
def version_command() -> None:
    version = importlib.metadata.version("daimon-adapter-cli")
    typer.echo(f"daimon {version}")


def main() -> None:
    """Console-script entry point: `daimon`.

    Usage errors and unhandled exceptions are printed redacted (no frame
    locals, credential text removed); exit codes are kept (2 for usage, 1
    for an unhandled error).
    """
    try:
        rc = app(standalone_mode=False)
    except click.exceptions.Exit as exc:
        raise SystemExit(exc.exit_code) from None
    except click.exceptions.Abort:
        print("Aborted!", file=sys.stderr)
        raise SystemExit(1) from None
    except click.ClickException as exc:
        buffer = io.StringIO()
        exc.show(file=buffer)
        print(redact_text(buffer.getvalue()), file=sys.stderr, end="")
        raise SystemExit(exc.exit_code) from None
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:
        trace = "".join(traceback.format_exception(exc))
        print(redact_text(trace), file=sys.stderr, end="")
        raise SystemExit(1) from None
    # Without standalone mode a command's typer.Exit(code=N) comes back as
    # the return value; keep it as the process exit code.
    if isinstance(rc, int) and rc:
        raise SystemExit(rc)


if __name__ == "__main__":
    main()
