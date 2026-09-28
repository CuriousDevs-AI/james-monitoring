# GitHub

Run the team from GitHub as well as the console, Telegram and Slack. There are two parts, and you can turn on either:

1. **Board mirror.** Every task becomes an issue on a GitHub Project board.
2. **Pull requests.** Code tasks open a real PR. Approving the request merges the PR.

Everything is done with the GitHub CLI (`gh`) logged in as **you**, so the issues, comments, PRs and merges are
yours. The team's commits also use your git identity (`owner.git_name` / `owner.git_email`, filled in from
`git config --global` at setup), with the teammate's name at the start of each commit message.

## Why a mirror, not a replacement

The task files in the team repo stay the engine, and GitHub is where you look at them and act on them:

- **Speed.** Agents read the board on every message. That's instant from files, but slow and rate-limited
  through the GitHub API, and impossible offline.
- **Rules.** One P0 per person, at most 2 tasks in progress, "Done means" before starting, and only you marking
  done. GitHub can't enforce these: anyone can drag a card to Done. With a mirror, your changes on GitHub go
  through the same rules as the console.
- **History.** Git keeps every change. Any model can read markdown.

## What happens where

| You do on GitHub | The team sees |
|---|---|
| Drag a card to **Done** (Stage or Status) | You accepted it: done, and whatever waited on it is unblocked |
| Drag a card from **Review** back to **Doing** | Changes requested: they're told now and rework it first |
| Change **Priority**, **Due**, **Owner** (a teammate's name) or **Project** | The same edit as in the console, with the same rules (e.g. one P0 per person) |
| **Comment** on the issue | Feedback, saved on the task and in their memory. On work in review, it sends it back |
| Move to **Blocked** and fill in **Blocker** | Blocked, and that person is told |

A change the rules refuse (a second P0, an unknown owner) is logged, and the board is set back to the real value.

The board fields are **Stage** (To do · Doing · Blocked · Review · Done · Cut), **Priority**, **Owner**, **Due**,
**Project** and **Blocker**. The built-in **Status** follows along (Todo / In Progress / Done), so the default board
view works. For the full picture, group a view by *Stage*.

## Set up (2 minutes)

1. Install and sign in to the GitHub CLI: `gh auth login`.
2. Give it access to Projects (once):

   ```bash
   gh auth refresh -s project
   ```

3. **Settings → GitHub** in the console:
   - **Owner**: your login (or your org).
   - **Repo for task issues**: `owner/name`. A private repo is best, e.g. your team repo.
   - **Project number**: leave it blank to create a new Project, or give an existing one.
   - Tick **Open code changes as pull requests** if you want PRs.
   - Click **Connect**. The board fields are created and every task is pushed within a couple of minutes.
     **Sync now** does it immediately.

```yaml
owner:
  name: Pankaj
  git_name: pankajneema
  git_email: you@example.com
github:
  owner: pankajneema          # or your org
  repo: CuriousDevs-AI/team   # where task issues live
  project: 7                  # github.com/users/pankajneema/projects/7
  sync_minutes: 2
  prs: true                   # code tasks: branch pushed, PR opened; approve = merge; reject = close
```

## Pull requests

With `prs: true`, and a project that has a code repo pushed to GitHub (`origin`):

1. The coding agent works on `jm/T-012` in a separate worktree. The commits are yours.
2. The branch is pushed and a PR is opened as you (`T-012: <task title>`, with the task's "Done means").
3. You get the usual 🔴 approval card, which links the PR.
   - **Approve**: the PR is merged on GitHub, the task is done, and your local `main` is fast-forwarded if it's clean.
   - **Reject**: the PR is closed with your reason.

If the merge fails (a conflict, a required check), the request stays open, so you can fix it and approve again.
