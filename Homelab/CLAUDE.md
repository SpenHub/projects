# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Personal homelab documentation and infrastructure-as-config for a single Unraid server. This is not a software project — there is no build, lint, or test tooling. Most of the value here is in `unraid/docs/inventory.md`, a hand-maintained snapshot of the server's state.

Note: this directory (`Homelab/`) lives inside a larger personal monorepo (`git@github.com:SpenHub/projects.git`); the git root is one level up, at the parent of `Homelab/`. Unrelated sibling projects (e.g. 3D modeling, other hobby files) exist in that monorepo's history but are outside this directory's scope.

## Structure

- `unraid/docs/inventory.md` — the current source of truth for the server: host details, array/disk status, shares, and the docker container table. It carries a "Last synced" date at the top — update that date whenever the inventory content changes.
- `unraid/docker/` — intended for docker-compose files / container configs (currently empty).
- `unraid/backup/` — intended for backup configuration/scripts (currently empty).
- `unraid/scripts/` — intended for maintenance/automation scripts (currently empty).

## Working with the inventory

`unraid/docs/inventory.md` is meant to reflect the real, current state of the Unraid host — not aspirational config. When asked to update it (e.g. after checking on the server), edit the relevant section in place and bump "Last synced" to the current date. The host is reachable via `ssh unraid` (configured in `~/.ssh/config`, key `~/.ssh/id_ed25519_unraid`).
