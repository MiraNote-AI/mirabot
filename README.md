# mirabot

Discord bot for the MiraNote team (MiraBot).

## Branches & environments

Two long-lived branches, standard across every MiraNote-AI repo:

- **`main` = prod.** Always demo-ready. Nothing is committed to it
  directly; it changes only through a `dev -> main` pull request that the
  team merges on purpose when we want a new stable/demo build.
- **`dev` = default branch.** All day-to-day work merges here first.

Flow:

```text
feature/<topic>  ->  PR into dev  ->  CI green  ->  merge into dev
      ...  (dev accumulates and integrates work)  ...
when we decide it is demo-ready:
dev  ->  PR into main  ->  merge  =>  prod updated
```

Open every new PR against `dev` (the default branch). `main` is touched
only by the deliberate `dev -> main` promotion PR, so prod always holds
exactly what the team chose to ship.
