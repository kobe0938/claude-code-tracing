# Trace Collection Methodology

## Overview
We collect end-to-end execution traces of the Claude Code agent on real software-engineering tasks. Each trace records every model API request and response issued during a single agent run on a single task. The corpus is produced by (i) executing Claude Code against tasks drawn from the SWE-bench Pro benchmark, (ii) intercepting all model traffic at a local LiteLLM proxy, and (iii) automating the agent lifecycle so that runs are isolated and reproducible.

## Benchmark Dataset
We use the public test split of **SWE-bench Pro** (`ScaleAI/SWE-bench_Pro`), loaded via the HuggingFace `datasets` library. For each instance we use three fields:
- `repo` — the GitHub repository slug (e.g. `NodeBB/NodeBB`),
- `base_commit` — the commit SHA representing the pre-fix state of the repository, and
- `problem_statement` — the natural-language issue description.

We deliberately **do not** supply the optional requirements/interface metadata bundled with the benchmark: the agent operates from the issue description alone, which is a more naturalistic and more challenging setting.

## Agent Configuration
All traces in the released corpus use a single fixed configuration we refer to as **Interactive + Plan + Yolo**:

- **Interactive mode.** Claude Code is launched as a TUI session (`claude --dangerously-skip-permissions`) rather than headless (`-p`). The TUI is required because plan mode and the subsequent execution phase are only exposed through it.
- **Plan mode.** The session is placed into plan mode (`/plan`) before the issue is sent. Plan mode triggers a read-only exploration phase in which Claude Code spawns parallel *Explore* subagents and a *Plan* agent to produce an implementation plan. Plan mode terminates with a mandatory user choice that bridges into the execution phase.
- **Yolo (`--dangerously-skip-permissions`).** At the plan/execute boundary we always select the option that continues execution with permissions bypassed, so the execution phase proceeds autonomously without per-tool prompts. (In Claude Code's documentation the terms "Yolo," "dangerously-skip-permissions," and "bypass permissions" refer to the same execution mode.)

The model is `claude-sonnet-4-6`, served by Anthropic and routed through the local proxy.

We selected this configuration after pilot runs across all four combinations of {Interactive, Headless} × {Restricted, Yolo}. Interactive + Plan + Yolo gave the most consistent completion behaviour and the most behaviourally rich traces (planning, multi-agent exploration, autonomous execution) at moderate cost — typically ~7–10 minutes wall-clock and ~100–120 model calls per task. Headless + Restricted, in contrast, silently rejected unpermitted tool calls and required ~50 minutes and ~450 model calls on the same task; Headless + Yolo skipped the planning phase entirely.

## Trace Capture Infrastructure
Claude Code does not expose a public trace export. To capture model-level traffic without modifying the agent, we point Claude Code at a **LiteLLM proxy** running on `localhost:4000` and attach a custom logging callback that writes one JSON line per request/response.

The proxy is configured with a single model alias forwarding to the upstream Anthropic API:

```yaml
model_list:
  - model_name: claude-sonnet-4-6
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_key: os.environ/ANTHROPIC_API_KEY

litellm_settings:
  callbacks: custom_callbacks.proxy_handler_instance
```

The callback is a subclass of `litellm.integrations.custom_logger.CustomLogger` that implements `async_log_success_event` and `async_log_failure_event`, appending one record per call to `traces.jsonl`. Each record contains: timestamp, model, the full message list seen by the proxy, the upstream response object, start and end times, prompt and completion token counts, and the raw request body forwarded to the provider (`additional_args.complete_input_dict`). Capturing the raw request body — rather than only the LiteLLM-normalized form — preserves Claude-Code-specific fields (system prompt blocks, tool definitions, cache control, beta flags) that are needed for downstream analysis.

Claude Code is pointed at the proxy via three environment variables:
```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_MODEL=claude-sonnet-4-6
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer <key>"
```

We adopted this design after evaluating several alternatives that did not meet our requirements during the study period: `claude-trace`, LangSmith, `claude-code-proxy`, MLflow's Claude auto-logger (which produced empty input/output fields), and LiteLLM with the Langfuse callback (incompatible SDK versions). The custom JSONL callback is the smallest configuration that captures the full request body with no information loss.

## Automation Pipeline
A driver script (`pipeline_tmux.py`) executes tasks sequentially, one at a time. For each task `i`:

1. **Reset.** Delete the previous workspace and truncate `traces.jsonl`, so the next run is captured in isolation.
2. **Workspace setup.** `git clone` the task's repository, `git checkout base_commit`, and write `problem_statement` to `problem_statement.md` in the repository root.
3. **Launch.** Start `claude --dangerously-skip-permissions` inside a fresh `tmux` session whose working directory is the freshly cloned repository.
4. **Plan phase.** Send the keystrokes `/plan<Enter>`, then send the problem statement (chunked into 200-character segments, with internal newlines stripped to a single line to avoid premature submission).
5. **Detect plan completion.** The driver polls the `tmux` pane until three conditions hold simultaneously for three consecutive checks: (a) the option menu (`"Would you like to proceed?"` or a `❯` selector on a numbered line) is visible at the bottom of the pane; (b) the trace file size has not grown since the previous poll; and (c) the TUI is not showing activity indicators (spinner glyphs, `"Thinking"`, `"Crunching"`, `"Brewing"`).
6. **Execute phase.** Send `Down` then `Enter` to select the second menu option (auto-accept, bypass permissions). The driver verifies the selection took effect by checking that the menu disappears from the pane *or* the trace file grows by at least 10 KB; it retries `Enter` up to ten times if neither condition is met.
7. **Detect execution completion.** Poll the trace file every 10 s; the run is considered finished once the file has stopped growing for 18 consecutive polls (≈3 minutes of silence) following at least 60 s of activity, or once the `claude` process exits. A 30-minute hard timeout per phase guards against indefinite hangs.
8. **Persist trace.** Copy `traces.jsonl` to `raw/swe_pro_task_<i>_interactive_plan_yolo_trail_1.jsonl`, then run `parse_traces_raw_request.py` to flatten the proxy log into a per-turn structured form, written to `parsed/` under the same filename.
9. **Teardown.** Send `/exit`, kill the `tmux` session, delete the workspace, and proceed to task `i + 1`.

Because the trace file, workspace, and `tmux` session are all torn down between tasks, every run is independent — there is no cross-task state leakage at the agent, repository, or proxy layer.

## Trace Format
Each file in `raw/` is the verbatim proxy log for one task: one JSON record per model call, in temporal order. A record contains the system prompt, full message history, tool definitions, model response (including tool calls), token usage, and timing. The corresponding file in `parsed/` flattens nested tool calls and assistant turns into a uniform per-turn schema suitable for downstream analysis.

## Corpus
The released corpus consists of **186 traces** drawn from SWE-bench Pro tasks 1–200 under the Interactive + Plan + Yolo configuration described above. The 14 missing instances correspond to runs in which the agent failed to launch, the upstream API returned a sustained `Overloaded` error, or the 30-minute per-phase timeout was reached without the trace stabilizing. Failed runs are not retried in the released corpus, to avoid biasing the distribution toward easier instances.

---

Experiment doc(keep updating): https://wide-preface-8a2.notion.site/Claude-Code-tracing-30c13b4793fb80d7a26ac1def9c71388?source=copy_link
