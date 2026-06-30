# Decision Log

Design decisions that rest on an experiment, a comparison, or a measurement are
recorded here as `YYYY-MM-DD-<topic>.md`, so the *why* survives long after the diff.

Each entry should capture:

- **Decision** — what was settled.
- **Rationale** — the philosophical fork and the key data behind the call.
- **Alternatives & why rejected** — often harder to reconstruct later than the decision itself.
- **Evidence** — links to the script / raw output / report that backs it (keep these reproducible).
- **Commit** — the implementing commit hash, filled in once done.

When a decision is backed by an experiment, reference it from the commit message:
`Decision: docs/decisions/<file>`.

> This log starts fresh with the open-source release. The pre-release design
> rationale lives in [`../philosophy.md`](../philosophy.md) and
> [`../design-system.md`](../design-system.md).
