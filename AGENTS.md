## GPU / memory scheduler (gpu-scheduler)

This Mac runs heavy jobs through `gsched`. Do not start GPU- or memory-heavy
processes directly (training, large inference, model servers, big data loads):
wrap them so the scheduler decides when they start.

Wrap the command (it runs in your own shell, so it works from any folder, ~/Documents included):

    gsched exec --project <project> --name <short-label> --mem <GiB> --gpu exclusive|shared|none \
      --checkpoint atomic --est <duration> --fail-closed -- <command...>

It waits until memory/GPU are free, then runs the command with its normal output and exit code.
Exit 75 = the scheduler was unavailable or gave up: report it, do not rerun the command unwrapped.
Run anything longer than a minute or two in the background (your shell tool's background mode),
then check on it; waiting in the queue costs no tokens.

Before starting: `gsched status --brief` shows free memory, the GPU and the queue.
While waiting: `gsched list` shows your job; `gsched get <JOB_ID>` says why it waits (`blocked_by`, `why`).
`gsched suggest --project <p> --name <n>` suggests --mem from past runs.

Rules:
- `--mem` is a reservation in GiB. Ask for the peak you expect; under-asking can push the Mac into swap.
- `--gpu exclusive` for MLX/Metal/llama.cpp work, `--gpu none` for CPU-only work (it can run beside a GPU job).
- `--checkpoint atomic` unless the program saves to $GSCHED_CHECKPOINT_DIR, writes READY,
  and exits 0 on SIGUSR1 (then `cooperative`). Model servers you can restart freely: `restartable`.
- Estimates over 2h go to the night queue (23:00-08:00) and wait for it. Ask the user before adding
  `--queue day` to run a long job now.
- Never kill processes you did not start. Never use `gsched cancel --terminate` without the user's OK.

If you have the gpu-scheduler MCP tools: `get_status`, then `submit_job` (returns a job id at once), then
`get_job` with `wait_s: 30` and `get_job_logs`. Do not loop rapidly.

If gsched says "probably inside an agent sandbox that blocks Unix sockets" (Codex's default sandbox does
this), rerun the gsched command with escalated permissions, or use the MCP tools. Never fall back to
running the heavy command unwrapped.

Alternative when the daemon has Full Disk Access (or the code lives outside ~/Documents, ~/Desktop,
~/Downloads): `gsched run ... -- <command>` returns a job id at once and the daemon runs it;
`gsched wait <JOB_ID>` then `gsched logs <JOB_ID> --tail 4000`.
