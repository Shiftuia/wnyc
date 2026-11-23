# 04 — Fable 5.2 angle, API gotchas, demo

## Official topic (slide in the room, 23.11 17:55): "Three ways into accessibility"
- Fun / Delight: "Make accessibility surprising, beautiful or fun." (example: maze game removing obstacles)
- Smart / Breakthrough: "An accessibility tool that wasn't possible before." (example: geography globe game for kids)
- Productive / Everyday: "Remove a real barrier someone hits every day." (example: rewrite a bank/ministry letter in plain words)
We are **Smart**. Open the demo with the slide's own words: "An accessibility tool that wasn't possible before." Avoid the slide's example ideas — many teams will build them.
Second sentence (the barrier, from tonight's data): "Only 1 in 3 NYC subway stations is accessible, and some of their elevators work barely more than half the time."

## Budget and session hygiene ($100 credits)
- The host's example game cost ~$30 for 3 sessions at effort high. Ours is data-heavy: `/clear` after each build step (data layer, agent, UI) and start the next with "read README + research/0X, continue from step N".
- Keep large data out of the context: never paste CSV/parquet contents; query and print aggregates only.
- Copy the working folder before every big change (`cp -r app app.bak-N`) so the last good version is always demoable.
- Register the project in the form during build time.

## Judging (from the event field guide), 25% each
New Capability · It Works (live) · Keep or Share · Clarity of Demo (topic + lane + what the model did, in 2 min). Rules: show the prompt first, then live demo, zero slides, built tonight. Register the project in the form DURING build time.

Lane: **Smart** ("something the previous model could not handle"). The claim: one plain-English question → the model plans and runs analysis over 6 messy public datasets (21M rides, 500 stations, elevator logs, building registry, rents), validates its own joins, reads its own map, and answers with reasons and caveats.

## The "new capability" hooks (from Anthropic's Fable 5.2 launch page and docs, 9 Nov 2026)
- Vision / chart reading jumped: Chartography (no tools) 73.4% vs 44.8% for Fable 5.1. → Self-check step: the app renders the map PNG and sends it back to the model: "Does this map agree with your top-3? Point out anything odd." Show this live — it is the clearest "new model" moment.
- "Research and analysis on more ambiguous, data-heavy tasks"; "keeps itself honest by building controls and flagging results that don't reproduce" → the agent prints join match rates and re-runs the top-3.
- Agentic search WANDR 82.1% vs 68.7%; orchestrator role (can delegate to smaller models) — optional: Haiku 5.5 for geocode/formatting, Fable for planning.

## API gotchas for claude-fable-5-2 (docs: platform.claude.com …/whats-new-fable-5-2)
- Adaptive thinking is always on: do NOT send `thinking: {type: enabled, budget_tokens}` or `disabled` → 400. Omit `thinking`; control depth with `effort` (low|medium|high|xhigh|max; default high). For a snappy live demo use `effort: "medium"`.
- Forced tool use is not supported: `tool_choice` any/tool → 400. Keep `auto`.
- Don't edit earlier turns (prefix check → 400 "block is bound to a different conversation"). Keep history append-only.
- 1M context, 128k output. Price $8/M in, $40/M out (fast mode 2x price, ~2.5x speed — worth it for the demo turn only).
- Claude Code on credits: create the key in the Console **Organization** (not personal), `export ANTHROPIC_API_KEY=...`, `claude --model claude-fable-5-2`.

## 2-minute demo script
0:00–0:20 Prompt on screen (the MAIN_PROMPT headline + the one-line user question). "Topic accessibility, lane Smart."
0:20–0:35 The question: "I use a wheelchair, $3,500, I work at 350 5th Ave." Submit.
0:35–1:10 Answer appears: top-3 + reasons + biggest risk. Expand "How I decided": SQL + join checks.
1:10–1:30 Self-check: model looks at the rendered map and comments.
1:30–1:50 Toggle "Accessibility deserts": cheap zones with no accessible station and slow WAV. One sentence: "Only 1 in 3 subway stations is accessible; for a wheelchair user, the affordable map of NYC shrinks to this."
1:50–2:00 Change work address to Bronx Hub → map recomputes. "Keep or share: send it to anyone apartment-hunting in a chair."
Have the first query cached/warm: run it once right before your slot.
