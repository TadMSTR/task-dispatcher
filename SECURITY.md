# Security

## Reporting

Open a GitHub issue. This is homelab infrastructure, not a hosted service; there is no
private disclosure channel and no SLA.

## Threat model

This process decides which agents start and constructs the argument vector they start
with. Its trust boundaries, in the order they matter:

**The agent roster is the privilege boundary.** Each entry may name a user the agent runs
as and a launcher it runs through. The loader validates every field against a closed set —
name shape, project directory prefix, user shape, launcher directory — and rejects the
*whole file* on any violation rather than honouring the valid part of it. Nothing derived
from task content ever reaches that table, and no value from a task file reaches a
subprocess spawn.

**`sudo` is the backstop, not this file.** The sudoers policy pins both the user and the
launcher path independently. Editing the roster cannot widen what `sudo` will execute.
The roster selects from what is already permitted.

**A malformed roster fails the tick.** It never degrades to an empty mapping. See the
README for why that specific degradation is the dangerous one.

**Credentials are resolved, never stored.** This repository contains no credential
values — only the names of environment variables read at launch. The dispatcher refuses
to launch when a required credential is unresolved or expired, rather than launching a
session that will fail confusingly later. `.gitleaks.toml` allowlists those *names* so
the scanner stays useful.

## Known accepted findings

Task summaries are interpolated into Matrix notification text. Markdown injection is
possible; HTML and JSON injection are not. Accepted under a trust model where the agents
writing task files are not adversarial. Marked `SECURITY[accepted]` at the call site.
