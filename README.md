# MFG-C2-095 — Aquaculture Environmental Early Warning Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: MFG

## Overview

This agent watches a single aquaculture site's own history. It takes in environmental
observations, feeding and treatment records, and past disease or disaster events; compares
today's readings and the public weather forecast against the patterns that have actually
preceded trouble *at that site*; and returns a non-definitive advisory with the signals and past
incidents it is reasoning from. Outcomes are then recorded back as structured feedback, so the
next comparison has more to work with.

It suits three moments in a farm's routine: the daily log, where an operator wants to know
whether today's readings look unusual against the site's own record; a typhoon approach or a
sharp temperature swing, where a site lead wants the farm's own precedent rather than generic
advice; and the aftermath of an actual event, where recording what happened is what makes the
next warning better.

The agent is built to advise, never to assert or to act:

- **Nothing it says is a diagnosis.** Severity is a ranked judgment (`critical` / `high` /
  `medium` / `low`) about how far conditions have departed from the site's own history, not a
  determination of what is wrong. Where a serious pattern appears, the output points the reader
  towards professional fish-disease consultation.
- **Every advisory names its evidence.** The narrative cites signal IDs and incident IDs, so a
  reader can go back to the observation or the past event behind a statement instead of taking
  the text on trust.
- **It never issues an actuation.** There is no feeding, water-gate or dosing control path.
  Suggested responses are text for a person to act on.
- **It degrades openly.** Language-model interpretation is optional. Without a model the agent
  keeps serving on a deterministic path and says so in `degradation_reason` — it does not stay
  silent and it does not fabricate the richer reading.
- **The output is allow-listed on the way out.** A sanitiser walks the whole response tree, and
  only a named set of fields — the narrative, the severity, the citation lists — is exempt from
  scalar sanitisation. Client-supplied text cannot ride out through an unexpected field.

The agent does not diagnose disease, replace professional or regulatory guidance, run its own
ocean-condition model, or operate farm equipment.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- caller authentication in front of the entry adapter;
- a durable incident store. `wire_services()` defaults to an in-memory implementation, which
  loses everything on restart — the accumulated site history is the whole point of this agent, so
  a real deployment injects a persistent store;
- optionally, `ANTHROPIC_API_KEY`. With it, the bundled client interprets signals and structures
  free-text records. Without it — or without the optional `anthropic` extra installed — the agent
  degrades to its deterministic path and keeps serving. Exactly those two conditions degrade;
  anything else (a renamed symbol, a wrong constructor, a broken transitive import) is treated as
  a wiring bug and propagates rather than being silently swallowed.

Runtime parameters read from `config/config.yaml`: `max_retry` (`3`), `timeout_seconds` (`30`),
`memory_enabled` (`false`) and `hitl.enabled` (`false`).

Species are configuration, not code: `config/species/{species_code}.yaml` carries each target
species' profile, and the shipped set covers finfish, crustaceans, bivalves and seaweed
(`madai`, `kuruma_ebi`, `magaki`, `nori`). Adding a species is a new file, not a code change.

Public weather and hazard forecasts are the only external data source. Regional feeds such as
sea-condition or red-tide services are out of scope for this version.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent runs inside the platform runtime and takes its secrets from the platform secret provider. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR, and the agent does not fall back to a degraded
mode when it is absent. Without the framework package installed, `src/api/server.py` cannot be
imported at all — it imports `framework.*` at module level, so start-up ends in `ImportError`
rather than in a partially working server. With the framework present, the server starts even
when no model key is provisioned: that is the documented degradation, and `/health` must not go
down with it. The test suite runs without a platform connection.

## Input

`POST /invoke` takes a JSON body with `input` and `session_id`. `input` is a **JSON string**
carrying a `mode` — one of `invoke`, `ingest` or `feedback` — plus `farm_id`, `species_code` and
a mode-specific `payload`. An unknown mode ends the invocation as `status=error` rather than
being guessed at.

| Mode | Purpose |
|---|---|
| `ingest` | Record environmental observations, interventions and past incidents |
| `invoke` | Compare current conditions against the site's history and the forecast, and return an advisory |
| `feedback` | Record what actually happened, so later comparisons improve |

Callers must be authenticated. When `INVOKE_AUTH_TOKEN` is set on the server environment, a
caller that upstream middleware did not vouch for must present it as a Bearer token and is then
promoted to `VERIFIED_EXTERNAL`; a mismatch is answered with a generic **401**. Middleware-
established trust is never demoted. With no token configured and no middleware, a request reaches
the graph as `ANONYMOUS` and the node trust gate ends the run — configure one of the two before
deploying. `GET /health` reports liveness.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "session_id": "demo-001",
    "input": "{\"mode\":\"invoke\",\"farm_id\":\"farm-001\",\"species_code\":\"madai\",\"payload\":{\"current_observations\":[{\"metric\":\"water_temp\",\"value\":31.5,\"observed_at\":\"2026-07-10\",\"pond_id\":\"p1\"}]}}"
  }'
```

## Output

The standard AgentCore envelope is preserved; `output` is shaped per mode — an advisory, an
ingest summary, or a feedback confirmation. An advisory looks like this:

```json
{
  "status": "success",
  "output": {
    "severity": "critical",
    "severity_label_ja": "重大（養殖区画全滅級リスクを含む可能性）",
    "narrative": "water_temp が critical 水準の逸脱を示しています(根拠: sig-water_temp-2026-07-10-farm-001)...",
    "cited_signal_ids": ["sig-water_temp-2026-07-10-farm-001"],
    "cited_incident_ids": []
  }
}
```

`degradation_reason` is surfaced to the caller when the run took the deterministic path, so a
thinner advisory is identifiable as a degraded one rather than read as a calm result. Values
above are illustrative — real output depends on the site's data.

## Security and Limitations

The entry node requires verified-external trust. Free-text payload fields are redacted for staff
names on the way in, on top of the framework's own PII scan. On the way out, the whole response
tree passes through an allow-list-based sanitiser: evidence-ID lookalikes are masked and
control-action phrasing is suppressed, with only the narrative, severity and citation fields
exempt from scalar sanitisation.

Known limits:

- **The staff-name redaction is an honorific heuristic** (name + 様/さん/君/氏), not a full
  Japanese name recogniser. A name written without an honorific will pass through. This matches
  the documented scope of the personal data this agent expects to see.
- **A new site has little to compare against.** Until the site's own history accumulates, advice
  falls back on general species knowledge; specificity grows with the record.
- **Only public weather and hazard data is consumed.** No ocean-condition model, no red-tide feed.
- **Severity is relative to the site's history**, not an absolute risk measure, and a citation
  proves a signal or incident exists — not that it explains today's readings.

Output is machine-generated and assistive. A suspected serious event belongs with a
fish-disease specialist, and no operational decision should rest on this advisory alone.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), accumulating site history
- Generation mode: `llm`, with a deterministic fallback path
- HITL: not enabled
- Persistence: no framework checkpointer; the incident store is injected and should be durable in
  production
- Secrets: `ANTHROPIC_API_KEY` (optional — absence is a documented degradation)
- External data: public weather and hazard forecasts

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection and without the `anthropic` extra — the model client is
imported inside `wire_services()` precisely so the suite does not need it.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the species-profile model and the
security design. `docs/03_test_spec.md` records the test specification and the verification
results, including a live-model run and the mutation checks behind the degradation contract.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
