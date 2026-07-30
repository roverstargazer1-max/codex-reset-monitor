# Issue tracker: GitHub

Issues and PRDs for this repository live in GitHub Issues. Use the `gh` CLI for issue operations.

## Conventions

- Create: `gh issue create`
- Read: `gh issue view <number> --comments`
- List: `gh issue list`
- Comment: `gh issue comment <number>`
- Label: `gh issue edit <number> --add-label <label>`
- Close: `gh issue close <number>`

Infer the repository from `git remote -v`.

## Pull requests as a triage surface

PRs as a request surface: no.

## Skill terminology

- “Publish to the issue tracker” means create a GitHub issue.
- “Fetch the relevant ticket” means read the issue and its comments.
- Wayfinder maps and child work items use GitHub issues, native sub-issues, and native dependencies where available.
