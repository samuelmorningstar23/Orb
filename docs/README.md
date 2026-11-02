# Orb Documentation Index

Start at the repo-root [`README.md`](../README.md) (overview + getting started).
For learning the system, the canonical corpus is [`ORB_MASTER.md`](ORB_MASTER.md)
plus the 41 per-subsystem guides in [`guide/`](guide). The full branch history and
how the codebase was consolidated is documented in [`../BRANCHES.md`](../BRANCHES.md).

## Learning the system

| Doc | What it covers |
|---|---|
| [`ORB_MASTER.md`](ORB_MASTER.md) | Master teaching doc: architecture, lifecycle traces, glossary, where-do-I-find-X |
| [`guide/`](guide) | 41 numbered deep-dive guides, including pilot operations and offline behavior |
| [`REBUILD_PLAN.md`](REBUILD_PLAN.md) | Dependency-ordered curriculum to rebuild Orb from scratch |
| [`ORB_TECHNICAL_MANUAL.md`](ORB_TECHNICAL_MANUAL.md) | Assembled technical manual (generated from [`manual_sections/`](manual_sections) via `_assemble.py`) |
| [`DATA_LAYER.md`](DATA_LAYER.md) | Database schema, migrations, backend seam |
| [`SAGE_AMBIENT_INTELLIGENCE.md`](SAGE_AMBIENT_INTELLIGENCE.md) | Ambient AI architecture: provider router, PHI governance, context bus |
| [`DESIGN.md`](../DESIGN.md) (root) | Design system: night-shift/OR theme, liquid glass, tokens |

## Ops / setup

| Doc | What it covers |
|---|---|
| [`SETUP.md`](SETUP.md) | Environment setup, model downloads, deployment profiles |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Configuration reference: settings, TLS, backups, encryption at rest |

## Machine learning

| Doc | What it covers |
|---|---|
| [`MODEL_TRAINING.md`](MODEL_TRAINING.md) | Training the Vigil CfC and Forecast LSTM |
| [`ORB_TRAINING_PLAYBOOK.md`](ORB_TRAINING_PLAYBOOK.md) | Model training & dataset playbook |
| [`VIGIL_LABELLED_EXTRACT.md`](VIGIL_LABELLED_EXTRACT.md) | Offline, owner-labelled non-sepsis extract contract, validation and research-only training |
| [`MODEL_CARD_forecast_lstm.md`](MODEL_CARD_forecast_lstm.md) | Forecast model card |

## Audits, analyses & history (point-in-time)

The current architecture and implementation audit is [Repository audit and implementation](REPOSITORY_AUDIT_2026-10.md). Historical reports below describe their original dates; use current code and the latest product rulings when a status conflicts.

The subsequent [offline implementation record](OFFLINE_IMPLEMENTATION_2026-10.md) documents the local modules added on 11 October, their permissions and switches, verification, and remaining work. The [completion and browser acceptance record](OFFLINE_COMPLETION_AND_BROWSER_2026-10.md) adds reliability fixes, real synthetic browser journeys and screenshot evidence. Older gap registers retain their original verdicts.

The [ML security and Sage latency follow-up](ML_SECURITY_AND_SAGE_LATENCY_2026-10.md) records the fresh ML runtime, complete resolved dependency scans, local model compatibility and measured browser answers. Its candidate-environment results supersede the earlier residual dependency findings, without modifying an existing appliance environment or granting clinical approval.

The [Sage prompt-cache speed fix](SAGE_SPEED_FIX_2026-10.md) records the subsequent
full-SWA cache controls, guarded local runtime configuration and refreshed timing
qualification. Earlier latency misses remain available in the preceding report.

| Doc | What it covers |
|---|---|
| [`ORB_ANALYSIS.md`](ORB_ANALYSIS.md) | Deep multi-agent system analysis (132 findings) |
| [`ORB_FULLSTACK_AUDIT_2026-07.md`](ORB_FULLSTACK_AUDIT_2026-07.md) | Full-stack audit, July 2026 |
| [`QA_AUDIT_2026-07-04.md`](QA_AUDIT_2026-07-04.md) | QA audit, 2026-07-04 |
| [`OMNISCIENT_AI_SCORECARD.md`](OMNISCIENT_AI_SCORECARD.md) | Ambient-AI scorecard |
| [`ORB_FIX_LOG.md`](ORB_FIX_LOG.md) | Historical fix log (paths are point-in-time) |
| [`ORB_V2_AGENTIC_CHANGELOG.md`](ORB_V2_AGENTIC_CHANGELOG.md) | v2 agentic-execution changelog |
| [`DISCOVERY_BASELINE.md`](DISCOVERY_BASELINE.md) | Evidence-based discovery baseline |
| [`ROADMAP.md`](ROADMAP.md) | Sequenced roadmap |
| [`OPUS_EXECUTION_PROMPT.md`](OPUS_EXECUTION_PROMPT.md) | Process artifact: execution prompt for an agent run |

## Business / fundraising

| Doc | What it covers |
|---|---|
| [`ORB_INVESTOR_PITCH.md`](ORB_INVESTOR_PITCH.md) | Investor pitch |
| [`ORB_CONTEXT_BRIEF.md`](ORB_CONTEXT_BRIEF.md) | Founder context brief |
| [`fundraising/`](fundraising) | Pitch deck, market research, investor targets |

## Archive (`docs/archive/`)

Large, auto-generated, superseded, or session-capture documents. Kept for
reference; not maintained.

| File | Notes |
|---|---|
| `ORB_DOCUMENTATION_2026-03.md` | March 2026 product doc (predates the v2 rebuild, superseded by `ORB_MASTER.md` + `guide/`) |
| `orb_gem_documentation.md` | Large auto-generated documentation dump (~477 KB) |
| `orb_system_overview.md` | Earlier system overview (referenced by some source comments) |
| `sage_ambient_coding_session.md` | Capture of an ambient coding session |
