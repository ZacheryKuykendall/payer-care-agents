"""Command line entry point.

``payer-agents demo`` is the one-command walkthrough: no arguments, no credentials,
no network. It prints the routing distribution, the gate that fired and the audit
trail for every turn, because the interesting part of this system is not the answer
text -- it is the decision about whether to produce one.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from payer_agents.config import RuntimeMode, Settings
from payer_agents.demo_scenarios import SCENARIOS, SCENARIOS_BY_KEY
from payer_agents.domain import Channel, MemberRequest, TurnResult
from payer_agents.orchestrator import Orchestrator, build

DEFAULT_PORT = 47821
BAR_WIDTH = 24


def _console() -> Console:
    return Console(highlight=False, soft_wrap=False)


def _banner(console: Console, orchestrator: Orchestrator) -> None:
    info = orchestrator.describe()
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    table.add_row("mode", f"[bold]{info['mode']}[/]")
    table.add_row("decision client", info["decision_client"])
    table.add_row("grounding judge", info["judge"])
    table.add_row("answer composer", info["composer"])
    table.add_row("taxonomy", info["taxonomy_version"])
    table.add_row("indexes", ", ".join(f"{k}={v} chunks" for k, v in info["indexes"].items()))
    table.add_row("mcp tools", str(len(info["tools"])))
    egress = info["egress_policy"]
    if egress:
        table.add_row(
            "egress policy",
            f"{egress['name']} / {egress['mode']} / default {egress['default_action']} "
            f"/ {len(egress['rules'])} rules",
        )
    console.print(
        Panel(
            table,
            title="[bold]Payer Care Agents[/] - synthetic data only, no real PHI",
            border_style="cyan",
        )
    )


def _bar(value: float, width: int = BAR_WIDTH) -> str:
    filled = round(min(1.0, max(0.0, value)) * width)
    return "#" * filled + "." * (width - filled)


def _routing_table(result: TurnResult, threshold: float) -> Table:
    routing = result.routing
    table = Table(box=None, pad_edge=False, show_header=True, header_style="dim")
    table.add_column("option", width=20)
    table.add_column("p", justify="right", width=6)
    table.add_column("", width=BAR_WIDTH)
    for option, probability in sorted(routing.probabilities.items(), key=lambda kv: -kv[1]):
        selected = option == routing.intent.value
        style = (
            "bold green"
            if selected and probability >= threshold
            else ("bold yellow" if selected else "dim")
        )
        table.add_row(
            Text(option, style=style),
            Text(f"{probability:.3f}", style=style),
            Text(_bar(probability), style=style),
        )
    return table


def _print_turn(
    console: Console, orchestrator: Orchestrator, title: str, text: str, result: TurnResult
) -> None:
    routing = result.routing
    threshold = orchestrator.settings.threshold_for(routing.intent.value)

    console.print()
    console.rule(f"[bold]{title}")
    console.print(Text(f'  "{text}"', style="italic cyan"))
    console.print()
    console.print(_routing_table(result, threshold))
    console.print(
        f"  urgency [bold]{routing.urgency:.2f}[/] ({routing.urgency_label})   "
        f"clinician-required [bold]{routing.clinician_required_probability:.2f}[/]   "
        f"injection [bold]{routing.injection_probability:.2f}[/]   "
        f"margin [bold]{routing.top_margin:.3f}[/]"
    )

    gate = next((e for e in result.audit_events if e["category"] == "gate"), {"detail": {}})
    grounding = next((e for e in result.audit_events if e["category"] == "grounding"), None)

    if result.automated:
        verdict = Text("  AUTOMATED", style="bold green")
        verdict.append(f"   gate: {gate['detail'].get('explanation', '')}", style="dim")
    else:
        verdict = Text(
            f"  HUMAN IN THE LOOP -> {result.handoff.queue} ({result.handoff.priority})",
            style="bold yellow",
        )
        why = gate["detail"].get("explanation") or result.handoff.context.get(
            "gate_explanation", ""
        )
        verdict.append(f"   reason: {result.handoff.reason.value} - {why}", style="dim")
    console.print(verdict)

    if grounding:
        detail = grounding["detail"]
        console.print(
            f"  grounding judge: [bold]{detail['score']:.2f}[/] / 3.00 "
            f"({detail['label']}), unsafe-content probability "
            f"{detail['unsafe_probability']:.2f}, threshold {detail['threshold']}",
            style="dim",
        )

    console.print()
    console.print(Panel(result.display_text, border_style="grey39", padding=(0, 1)))

    tool_calls = result.answer.tool_calls if result.answer else []
    if tool_calls:
        for call in tool_calls:
            console.print(
                f"  tool [bold]{call.tool}[/] -> {call.gateway_decision}, "
                f"{call.duration_ms:.1f} ms, correlation {call.correlation_id}",
                style="dim",
            )
    console.print(
        f"  audit records: {len(result.audit_events)} "
        f"({', '.join(e['category'] for e in result.audit_events)})",
        style="dim",
    )
    console.print(f"  total latency: {result.total_latency_ms:.1f} ms", style="dim")


# -- commands ---------------------------------------------------------------


def cmd_demo(args: argparse.Namespace) -> int:
    console = _console()
    orchestrator = build(_settings(args))
    _banner(console, orchestrator)

    scenarios = SCENARIOS
    if args.only:
        missing = [k for k in args.only if k not in SCENARIOS_BY_KEY]
        if missing:
            console.print(f"[red]unknown scenario(s): {', '.join(missing)}[/]")
            return 2
        scenarios = [SCENARIOS_BY_KEY[k] for k in args.only]

    automated = 0
    for scenario in scenarios:
        result = orchestrator.handle(MemberRequest(text=scenario.text, channel=scenario.channel))
        automated += int(result.automated)
        _print_turn(console, orchestrator, scenario.title, scenario.text, result)
        console.print(f"  [dim]why this is here:[/] {scenario.teaches}")

    console.print()
    console.rule("[bold]Summary")
    console.print(
        f"  {automated} of {len(scenarios)} turns answered automatically; "
        f"{len(scenarios) - automated} handed to a human."
    )
    console.print(
        f"  audit chain verified: "
        f"[bold]{'yes' if orchestrator.audit.verify() else 'NO'}[/] "
        f"({len(orchestrator.audit.events)} records)"
    )
    console.print(
        "  every number above was produced locally by the offline model. See docs/EVALUATION.md.",
        style="dim",
    )
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    console = _console()
    orchestrator = build(_settings(args))
    result = orchestrator.handle(
        MemberRequest(text=args.text, member_id=args.member_id, channel=Channel(args.channel))
    )
    if args.json:
        print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
        return 0
    _banner(console, orchestrator)
    _print_turn(console, orchestrator, "Ad hoc request", args.text, result)
    return 0


def cmd_evals(args: argparse.Namespace) -> int:
    from payer_agents.evals.runner import run_suite

    console = _console()
    orchestrator = build(_settings(args))
    report = run_suite(orchestrator)

    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2, default=str))
        return 0

    console.rule("[bold]Evaluation")
    for line in report.summary_lines():
        console.print(f"  {line}")

    console.print()
    console.print("[bold]Threshold sweep[/] (what each operating point buys and costs)")
    sweep = Table(box=None, header_style="dim")
    sweep.add_column("threshold", justify="right")
    sweep.add_column("automated", justify="right")
    sweep.add_column("deferred", justify="right")
    sweep.add_column("automation rate", justify="right")
    sweep.add_column("error rate on automated", justify="right")
    for point in report.threshold_points:
        sweep.add_row(
            f"{point.threshold:.2f}",
            str(point.automated),
            str(point.deferred),
            f"{point.automation_rate:.0%}",
            f"{point.automated_error_rate:.1%}",
        )
    console.print(sweep)

    misses = [o for o in report.outcomes if not o.correct]
    if misses:
        console.print()
        console.print("[bold]Routing misses[/]")
        for outcome in misses:
            console.print(
                f"  {outcome.id}: expected {outcome.expected}, got {outcome.predicted} "
                f"at {outcome.confidence:.2f} "
                f"(handed to a human: {not outcome.automated})"
            )

    console.print()
    for note in report.notes:
        console.print(f"  [dim]note:[/] {note}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from payer_agents.api.app import create_app

    app = create_app(settings=_settings(args))
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    console = _console()
    orchestrator = build(_settings(args))
    assert orchestrator.registry is not None
    gateway_policy = orchestrator.mcp.gateway.policy

    table = Table(title="MCP tools", header_style="dim")
    table.add_column("tool")
    table.add_column("risk")
    table.add_column("writes")
    table.add_column("gateway")
    for tool in orchestrator.registry.list():
        permitted = gateway_policy.permits(tool.name)
        table.add_row(
            tool.name,
            tool.risk.value,
            "yes" if tool.writes else "no",
            "[green]allowed[/]" if permitted else "[red]blocked[/]",
        )
    console.print(table)
    return 0


def cmd_egress(args: argparse.Namespace) -> int:
    console = _console()
    orchestrator = build(_settings(args))
    policy = orchestrator.egress_policy
    assert policy is not None

    table = Table(title=f"Egress policy: {policy.name} ({policy.mode.value})", header_style="dim")
    table.add_column("url")
    table.add_column("action")
    table.add_column("rule")
    table.add_column("outcome")
    for url in args.urls:
        decision = policy.evaluate(url)
        table.add_row(
            url,
            decision.action.value,
            decision.rule_name,
            "[green]allowed[/]" if decision.allowed else "[red]blocked[/]",
        )
    console.print(table)
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    console = _console()
    orchestrator = build(_settings(args))
    for scenario in SCENARIOS:
        orchestrator.handle(MemberRequest(text=scenario.text, channel=scenario.channel))
    events = orchestrator.audit.as_dicts()
    if args.json:
        print(json.dumps(events, indent=2, default=str))
        return 0

    table = Table(title=f"Audit trail ({len(events)} records)", header_style="dim")
    table.add_column("#", justify="right")
    table.add_column("category")
    table.add_column("action")
    table.add_column("outcome")
    table.add_column("hash", style="dim")
    for event in events[-args.limit :]:
        table.add_row(
            str(event["sequence"]),
            event["category"],
            event["action"],
            event["outcome"],
            event["hash"][:12],
        )
    console.print(table)
    console.print(f"  hash chain valid: [bold]{'yes' if orchestrator.audit.verify() else 'NO'}[/]")
    return 0


# -- plumbing ---------------------------------------------------------------


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "mode", None):
        settings.mode = RuntimeMode(args.mode)
        if settings.mode is RuntimeMode.AZURE:
            settings.require_azure()
    return settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="payer-agents",
        description=("Governed multi-agent healthcare payer assistant. Synthetic data only."),
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in RuntimeMode],
        help="override PAYER_AGENTS_MODE",
    )
    sub = parser.add_subparsers(dest="command")

    demo = sub.add_parser("demo", help="run the scripted walkthrough (default)")
    demo.add_argument(
        "--only",
        nargs="*",
        metavar="KEY",
        help=f"scenario keys to run: {', '.join(SCENARIOS_BY_KEY)}",
    )
    demo.set_defaults(func=cmd_demo)

    ask = sub.add_parser("ask", help="send one request")
    ask.add_argument("text")
    ask.add_argument("--member-id")
    ask.add_argument(
        "--channel",
        default=Channel.MEMBER_PORTAL.value,
        choices=[c.value for c in Channel],
    )
    ask.add_argument("--json", action="store_true")
    ask.set_defaults(func=cmd_ask)

    evals = sub.add_parser("evals", help="run the labelled evaluation suite")
    evals.add_argument("--json", action="store_true")
    evals.set_defaults(func=cmd_evals)

    serve = sub.add_parser("serve", help="start the demo console and APIs")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=cmd_serve)

    tools = sub.add_parser("tools", help="list MCP tools and their gateway status")
    tools.set_defaults(func=cmd_tools)

    egress = sub.add_parser("egress", help="evaluate URLs against the egress policy")
    egress.add_argument("urls", nargs="+")
    egress.set_defaults(func=cmd_egress)

    audit = sub.add_parser("audit", help="run the demo and dump the audit trail")
    audit.add_argument("--limit", type=int, default=40)
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args([*(argv or []), "demo"])
    handler: Any = args.func
    return int(handler(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
