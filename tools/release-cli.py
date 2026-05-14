#!/usr/bin/env python3
"""Interactive release CLI for ha-nanit.

Single entry point for the full release lifecycle:
  create PR → tag → merge → release beta → release stable

Usage: python tools/release-cli.py
       just release
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
except ImportError:
    print("\033[31mError: 'rich' required. Run: pip install rich\033[0m")
    sys.exit(1)

console = Console()

# ─── Config (repo-specific) ──────────────────────────────────────────

REPO_NAME = "ha-nanit"
RELEASE_WORKFLOW = "release.yaml"
MAIN_BRANCH = "main"

# ─── Data model ──────────────────────────────────────────────────────

BETA_RE = re.compile(r"^v(\d+\.\d+\.\d+)-beta\.(\d+)$")
STABLE_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")


@dataclass
class BetaInfo:
    tag: str  # v1.4.0-beta.1
    version: str  # 1.4.0
    beta_num: int  # 1
    date: str  # 2026-05-10
    released: bool  # has GitHub pre-release


@dataclass
class StableInfo:
    tag: str  # v1.4.0
    date: str  # 2026-05-10
    latest: bool  # is the latest stable


@dataclass
class State:
    branch: str = ""
    ahead: int = 0
    on_main: bool = False
    latest_stable: StableInfo | None = None
    stables: list[StableInfo] = field(default_factory=list)
    betas: list[BetaInfo] = field(default_factory=list)
    pr: dict[str, Any] | None = None

    @property
    def unreleased_betas(self) -> list[BetaInfo]:
        return [b for b in self.betas if not b.released]

    @property
    def released_betas(self) -> list[BetaInfo]:
        return [b for b in self.betas if b.released]

    @property
    def promotable_versions(self) -> dict[str, BetaInfo]:
        """Versions with beta tags not yet promoted to stable."""
        stable_versions = {s.tag.lstrip("v") for s in self.stables}
        versions: dict[str, BetaInfo] = {}
        for beta in self.betas:
            if beta.version not in stable_versions and (
                beta.version not in versions or beta.beta_num > versions[beta.version].beta_num
            ):
                versions[beta.version] = beta
        return versions

    @property
    def pr_release_label(self) -> str | None:
        if not self.pr:
            return None
        for label in self.pr.get("labels", []):
            if label.startswith("release:"):
                return label
        return None

    @property
    def pr_has_release_label(self) -> bool:
        return self.pr_release_label is not None


# ─── Shell helpers ───────────────────────────────────────────────────


async def sh(cmd: str) -> str:
    """Run shell command, return stdout or empty string on failure."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip() if proc.returncode == 0 else ""


async def sh_ok(cmd: str) -> bool:
    """Run shell command, return True if exit code 0."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0


# ─── State fetching ──────────────────────────────────────────────────


async def fetch_state() -> State:
    """Fetch all release state in one parallel batch."""
    state = State()

    # All network + git calls fire in parallel
    branch, ahead, tags_raw, releases_json, pr_json, _ = await asyncio.gather(
        sh("git branch --show-current"),
        sh("git rev-list --count main..HEAD 2>/dev/null"),
        sh(
            "git for-each-ref --sort=version:refname "
            "--format='%(refname:short) %(creatordate:short)' 'refs/tags/v*'"
        ),
        sh("gh release list --limit 100 --json tagName,isPrerelease,isDraft,publishedAt"),
        sh(
            "gh pr list --state open "
            '--head "$(git branch --show-current)" '
            "--json number,labels,title,url --limit 1"
        ),
        sh("git fetch origin --tags --quiet"),
    )

    state.branch = branch or "detached"
    state.ahead = int(ahead) if ahead.isdigit() else 0
    state.on_main = state.branch == MAIN_BRANCH

    # Parse tag dates from git
    tag_dates: dict[str, str] = {}
    for line in (tags_raw or "").split("\n"):
        line = line.strip().strip("'")
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            tag_dates[parts[0]] = parts[1]

    # Parse releases from GitHub
    released_tags: set[str] = set()
    release_dates: dict[str, str] = {}
    releases = json.loads(releases_json) if releases_json else []

    for r in releases:
        tag = r["tagName"]
        is_pre = r.get("isPrerelease", False)
        is_draft = r.get("isDraft", False)
        date = r.get("publishedAt", "")[:10]
        released_tags.add(tag)
        release_dates[tag] = date

        if not is_pre and not is_draft and STABLE_RE.match(tag):
            info = StableInfo(
                tag=tag,
                date=date,
                latest=state.latest_stable is None,
            )
            state.stables.append(info)
            if state.latest_stable is None:
                state.latest_stable = info

    # Parse beta tags from git
    stable_versions = {s.tag.lstrip("v") for s in state.stables}
    for tag, date in tag_dates.items():
        m = BETA_RE.match(tag)
        if m:
            version = m.group(1)
            if version in stable_versions:
                continue
            state.betas.append(
                BetaInfo(
                    tag=tag,
                    version=version,
                    beta_num=int(m.group(2)),
                    date=release_dates.get(tag, date),
                    released=tag in released_tags,
                )
            )

    # Sort betas by version descending
    state.betas.sort(
        key=lambda b: ([int(p) for p in b.version.split(".")], b.beta_num),
        reverse=True,
    )

    # Parse PR
    if pr_json:
        try:
            prs = json.loads(pr_json)
            if isinstance(prs, list) and prs:
                pr = prs[0]
                state.pr = {
                    "number": pr["number"],
                    "title": pr["title"],
                    "url": pr["url"],
                    "labels": [lbl["name"] for lbl in pr.get("labels", [])],
                }
        except (json.JSONDecodeError, KeyError):
            pass

    return state


# ─── Dashboard ───────────────────────────────────────────────────────


def render_dashboard(state: State) -> Panel:
    """Render the status dashboard panel."""
    lines: list[str] = []

    # Latest stable
    if state.latest_stable:
        lines.append(
            f"  [bold green]stable [/] "
            f"[bold]{state.latest_stable.tag}[/]  "
            f"[dim]({state.latest_stable.date})[/]"
        )
    else:
        lines.append("  [bold green]stable [/] [dim]none[/]")

    # Active betas
    released = state.released_betas
    if released:
        tags = "  ".join(f"[yellow]{b.tag}[/]" for b in released[:3])
        extra = f"  [dim]+{len(released) - 3} more[/]" if len(released) > 3 else ""
        lines.append(f"  [bold yellow]betas  [/] {tags}{extra}")

    unreleased = state.unreleased_betas
    if unreleased:
        tags = "  ".join(f"[cyan]{b.tag}[/]" for b in unreleased[:3])
        extra = f"  [dim]+{len(unreleased) - 3} more[/]" if len(unreleased) > 3 else ""
        lines.append(f"  [bold cyan]pending[/] {tags}{extra}")

    lines.append("")

    # Current branch
    if not state.on_main:
        ahead_str = f"  [dim]({state.ahead} ahead of {MAIN_BRANCH})[/]" if state.ahead else ""
        lines.append(f"  [bold blue]branch [/] {state.branch}{ahead_str}")
    else:
        lines.append(f"  [bold blue]branch [/] {state.branch}")

    # PR status
    if state.pr:
        label_str = ""
        release_label = state.pr_release_label
        if release_label:
            label_str = f"  [green]({release_label})[/]"
        lines.append(
            f"  [bold magenta]pr     [/] "
            f"[bold]#{state.pr['number']}[/] {state.pr['title']}{label_str}"
        )

    content = "\n".join(lines)
    return Panel(
        content,
        title=f"[bold]{REPO_NAME}[/]",
        border_style="bright_blue",
        padding=(1, 2),
    )


# ─── Menu ────────────────────────────────────────────────────────────


@dataclass
class MenuItem:
    key: str
    label: str
    description: str
    action: str


def _version_bump(version: str, bump: str) -> str:
    """Compute bumped version string."""
    parts = [int(p) for p in version.split(".")]
    if bump == "patch":
        parts[2] += 1
    elif bump == "minor":
        parts[1] += 1
        parts[2] = 0
    elif bump == "major":
        parts[0] += 1
        parts[1] = 0
        parts[2] = 0
    return ".".join(str(p) for p in parts)


def build_menu(state: State) -> list[MenuItem]:
    """Build context-aware menu items."""
    items: list[MenuItem] = []

    if not state.on_main and not state.pr and state.ahead > 0:
        items.append(MenuItem("p", "Create PR", "push & open PR with release label", "create_pr"))

    if state.pr and not state.pr_has_release_label:
        items.append(MenuItem("t", "Tag PR", "add release label to current PR", "tag_pr"))

    if state.pr:
        items.append(MenuItem("m", "Merge PR", "squash-merge → triggers auto-beta", "merge_pr"))

    if state.unreleased_betas:
        items.append(
            MenuItem(
                "b",
                "Release beta",
                "publish pre-release → PyPI beta",
                "release_beta",
            )
        )

    if state.promotable_versions:
        items.append(MenuItem("s", "Release stable", "ship to production", "release_stable"))

    items.append(MenuItem("v", "View releases", "release history & status", "view_releases"))
    items.append(MenuItem("r", "Retry pipeline", "re-trigger failed release workflow", "retry"))

    return items


def show_menu(items: list[MenuItem]) -> str:
    """Display menu and return selected action name."""
    console.print()
    for item in items:
        console.print(
            f"  [bold cyan]{item.key}[/])  [bold]{item.label:<16}[/][dim]{item.description}[/]"
        )
    console.print("  [dim]q)  Quit[/]")
    console.print()

    valid = {item.key: item.action for item in items}
    valid["q"] = "quit"

    while True:
        choice = console.input("  [bold]▸ [/]").strip().lower()
        if choice in valid:
            return valid[choice]
        console.print("  [red]Invalid choice.[/]")


# ─── Actions ─────────────────────────────────────────────────────────


def _ask_bump(state: State) -> str | None:
    """Ask for release bump type. Returns label or None."""
    current = state.latest_stable.tag.lstrip("v") if state.latest_stable else "0.0.0"

    console.print("\n  [bold]Include in a release?[/]")
    console.print(
        f"    [cyan]1)[/]  patch  [dim]({current} → {_version_bump(current, 'patch')})[/]"
    )
    console.print(
        f"    [cyan]2)[/]  minor  [dim]({current} → {_version_bump(current, 'minor')})[/]"
    )
    console.print(
        f"    [cyan]3)[/]  major  [dim]({current} → {_version_bump(current, 'major')})[/]"
    )
    console.print("    [dim]4)[/]  no release")

    label_map = {"1": "release:patch", "2": "release:minor", "3": "release:major"}
    choice = Prompt.ask("\n  [bold]Select[/]", choices=["1", "2", "3", "4"])
    return label_map.get(choice)


async def action_create_pr(state: State) -> None:
    """Create a PR for the current branch."""
    console.print()

    last_commit = await sh("git log -1 --format='%s'")
    title = Prompt.ask("  [bold]PR title[/]", default=last_commit)

    label = _ask_bump(state)

    with console.status("  [bold]Pushing branch..."):
        push_ok = await sh_ok(f"git push -u origin {state.branch}")
    if not push_ok:
        console.print("  [red]✗ Failed to push branch.[/]")
        return
    console.print("  [green]✓[/] Branch pushed")

    label_flag = f'--label "{label}"' if label else ""
    with console.status("  [bold]Creating PR..."):
        result = await sh(
            f'gh pr create --title "{title}" --body "" --base {MAIN_BRANCH} {label_flag}'
        )

    if result:
        console.print(f"  [green]✓[/] {result}")
    else:
        console.print("  [red]✗ Failed to create PR.[/]")


async def action_tag_pr(state: State) -> None:
    """Add a release label to an existing PR."""
    if not state.pr:
        return

    console.print(f"\n  PR [bold]#{state.pr['number']}[/]: {state.pr['title']}")

    label = _ask_bump(state)
    if not label:
        console.print("  [dim]No label selected.[/]")
        return

    with console.status("  [bold]Adding label..."):
        ok = await sh_ok(f'gh pr edit {state.pr["number"]} --add-label "{label}"')

    if ok:
        console.print(f"  [green]✓[/] Label [bold]{label}[/] added to PR #{state.pr['number']}")
    else:
        console.print("  [red]✗ Failed to add label.[/]")


async def action_merge_pr(state: State) -> None:
    """Squash-merge the current PR."""
    if not state.pr:
        return

    console.print(f"\n  Merging PR [bold]#{state.pr['number']}[/]: {state.pr['title']}")

    if state.pr_has_release_label:
        console.print(f"  Label: [green]{state.pr_release_label}[/] → auto-beta will tag on merge")
    else:
        console.print("  [yellow]No release label — no beta tag will be created[/]")

    if not Confirm.ask("\n  Squash-merge?"):
        return

    with console.status("  [bold]Merging..."):
        ok = await sh_ok(f"gh pr merge {state.pr['number']} --squash --delete-branch")

    if ok:
        console.print("  [green]✓[/] PR merged")
        if state.pr_has_release_label:
            console.print(
                "  [dim]auto-beta.yaml will tag shortly. Refresh to see new beta tags.[/]"
            )
    else:
        console.print("  [red]✗ Failed to merge. Check CI status and approvals.[/]")


async def action_release_beta(state: State) -> None:
    """Create a GitHub pre-release for an unreleased beta tag."""
    unreleased = state.unreleased_betas
    if not unreleased:
        console.print("  [yellow]No unreleased beta tags.[/]")
        return

    console.print("\n  [bold]Unreleased beta tags:[/]")
    for i, beta in enumerate(unreleased, 1):
        console.print(f"    [cyan]{i})[/]  {beta.tag}  [dim](tagged {beta.date})[/]")

    choice = Prompt.ask(
        "\n  [bold]Release which beta[/]",
        choices=[str(i) for i in range(1, len(unreleased) + 1)],
    )
    selected = unreleased[int(choice) - 1]

    console.print(f"\n  Releasing [bold]{selected.tag}[/] as pre-release")
    if not Confirm.ask("  Confirm?"):
        return

    with console.status("  [bold]Creating release..."):
        ok = await sh_ok(
            f'gh release create "{selected.tag}" '
            f'--title "{selected.tag}" '
            f"--generate-notes --prerelease --latest=false"
        )

    if ok:
        console.print(f"  [green]✓[/] Pre-release {selected.tag} created → PyPI publish triggered")
    else:
        console.print("  [red]✗ Failed to create release.[/]")


async def action_release_stable(state: State) -> None:
    """Promote a beta to stable release."""
    versions = state.promotable_versions
    if not versions:
        console.print("  [yellow]No versions to promote.[/]")
        return

    items = sorted(
        versions.items(),
        key=lambda x: [int(p) for p in x[0].split(".")],
        reverse=True,
    )

    console.print("\n  [bold]Available versions:[/]")
    for i, (ver, beta) in enumerate(items, 1):
        tested = "[green]✓ beta-tested[/]" if beta.released else "[dim]untested[/]"
        console.print(
            f"    [cyan]{i})[/]  [bold]v{ver}[/]  [dim](from {beta.tag}, {beta.date})[/]  {tested}"
        )

    choice = Prompt.ask(
        "\n  [bold]Release which version[/]",
        choices=[str(i) for i in range(1, len(items) + 1)],
    )
    ver, beta = items[int(choice) - 1]

    sha = await sh(f"git rev-list -n1 {beta.tag}")
    if not sha:
        console.print(f"  [red]Could not resolve commit for {beta.tag}[/]")
        return

    console.print(f"\n  [bold]{beta.tag} → v{ver}[/]")
    console.print(f"  Commit: [dim]{sha[:7]}[/]")
    if not Confirm.ask("\n  Confirm release?"):
        return

    with console.status("  [bold]Creating tag..."):
        await sh(f'git tag -m "v{ver}" "v{ver}" "{sha}"')
        push_ok = await sh_ok(f'git push origin "v{ver}"')

    if not push_ok:
        console.print("  [red]✗ Failed to push tag.[/]")
        return

    with console.status("  [bold]Creating GitHub release..."):
        ok = await sh_ok(f'gh release create "v{ver}" --title "v{ver}" --generate-notes --latest')

    if ok:
        console.print(
            f"  [green]✓[/] v{ver} released → CI gate + PyPI publish + artifact attachment"
        )
    else:
        console.print("  [red]✗ Failed to create GitHub release.[/]")


async def action_view_releases(state: State) -> None:
    """Show release history."""
    table = Table(
        box=box.ROUNDED,
        border_style="bright_blue",
        title="releases",
        title_style="bold",
    )
    table.add_column("Version", style="bold")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Date", style="dim")

    for s in state.stables[:10]:
        status = "[bold green]latest[/]" if s.latest else "previous"
        table.add_row(s.tag, "[green]stable[/]", status, s.date)

    for b in state.released_betas:
        table.add_row(b.tag, "[yellow]beta[/]", "[yellow]released[/]", b.date)

    for b in state.unreleased_betas:
        table.add_row(b.tag, "[cyan]tag[/]", "[dim]pending[/]", b.date)

    if not state.stables and not state.betas:
        console.print("\n  [dim]No releases yet.[/]")
    else:
        console.print()
        console.print(table)


async def action_retry(state: State) -> None:
    """Re-trigger the release workflow for a tag."""
    releases_json = await sh("gh release list --limit 10 --json tagName,publishedAt")
    releases = json.loads(releases_json) if releases_json else []

    if not releases:
        console.print("  [yellow]No releases found.[/]")
        return

    console.print("\n  [bold]Recent releases:[/]")
    for i, r in enumerate(releases, 1):
        console.print(f"    [cyan]{i})[/]  {r['tagName']}  [dim]({r['publishedAt'][:10]})[/]")

    choice = Prompt.ask(
        "\n  [bold]Retry which release[/]",
        choices=[str(i) for i in range(1, len(releases) + 1)],
    )
    tag = releases[int(choice) - 1]["tagName"]

    if not Confirm.ask(f"  Re-trigger {RELEASE_WORKFLOW} for [bold]{tag}[/]?"):
        return

    with console.status("  [bold]Dispatching..."):
        ok = await sh_ok(f'gh workflow run {RELEASE_WORKFLOW} -f tag_name="{tag}"')

    if ok:
        console.print(f"  [green]✓[/] Workflow dispatched for {tag}")
    else:
        console.print("  [red]✗ Failed to dispatch workflow.[/]")


# ─── Main loop ───────────────────────────────────────────────────────

ACTIONS: dict[str, Any] = {
    "create_pr": action_create_pr,
    "tag_pr": action_tag_pr,
    "merge_pr": action_merge_pr,
    "release_beta": action_release_beta,
    "release_stable": action_release_stable,
    "view_releases": action_view_releases,
    "retry": action_retry,
}


async def main_loop() -> None:
    """Main interactive loop."""
    with console.status("  [bold]Loading...", spinner="dots"):
        state = await fetch_state()

    while True:
        console.clear()
        console.print(render_dashboard(state))

        items = build_menu(state)
        action = show_menu(items)

        if action == "quit":
            break

        handler = ACTIONS.get(action)
        if handler:
            await handler(state)

        console.print()
        console.input("  [dim]Press Enter to continue[/]")

        with console.status("  [bold]Refreshing...", spinner="dots"):
            state = await fetch_state()


def main() -> None:
    """Entry point."""
    for cmd in ("git", "gh"):
        if not shutil.which(cmd):
            console.print(f"[red]Error: '{cmd}' not found in PATH.[/]")
            sys.exit(1)

    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        console.print("\n[dim]Bye.[/]")


if __name__ == "__main__":
    main()
