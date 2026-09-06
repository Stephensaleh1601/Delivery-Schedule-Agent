# Finalist Submission Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the GitHub submission reliable, truthful and immediately understandable in a five-minute judged demo.

**Architecture:** Preserve the current LLM-controlled LangGraph loop and deterministic planning core. Tighten provider boundaries and persistence, then make the existing confirmed-route evidence the dominant UI state. Treat documentation and tests as executable claims about the same demo.

**Tech Stack:** Python, FastAPI, Pydantic, LangGraph, AWS Bedrock, OR-Tools, SQLite, Next.js, TypeScript, Playwright, GitHub Actions

**Spec:** `docs/superpowers/specs/2026-09-06-finalist-submission-readiness-design.md`

## Global Constraints

- Branch and PR only. Never merge or push directly to `main`.
- GitHub `origin/main@4256a5c` is the baseline.
- Floof interview facts and demo assumptions must be labelled separately.
- The test suite must remain offline when `LLM_PROVIDER=none`.
- Deterministic planning code remains the source of route and promise truth.

---

### Task 1: Provider boundary and failing test contracts

**Files:**
- Modify: `dispatch_agent/config.py`
- Modify: `dispatch_agent/llm.py`
- Modify: `tests/test_depot_config.py`
- Create: `tests/test_llm.py`

- [ ] Add tests that reject unknown provider names and prove `none` constructs no SDK client.
- [ ] Run the tests and confirm the current fall-through fails.
- [ ] Add an explicit disabled client/error and provider validation.
- [ ] Run the focused tests, then the previously failing contract tests.

### Task 2: Persistence and redaction guarantees

**Files:**
- Modify: `dispatch_agent/db.py`
- Modify: `dispatch_agent/planning/offer_service.py`
- Modify: `dispatch_agent/planning/tools.py`
- Modify: `dispatch_agent/agents/scheduling_agent.py`
- Modify: `dispatch_agent/webapp/chat_api.py`
- Modify: `dispatch_agent/webapp/main.py`
- Modify: focused tests under `tests/`

- [ ] Add failing tests for replayed client keys, failed acceptance rollback and token-shaped exceptions.
- [ ] Implement stored event lookup, transaction-scoped acceptance and redaction at persistence boundaries.
- [ ] Run focused tests and the full Python suite.

### Task 3: Five-minute chat experience

**Files:**
- Modify: `frontend/src/app/chat/page.tsx`
- Modify: `frontend/src/components/AgentDecision.tsx`
- Modify: `frontend/src/components/AgentProgress.tsx`
- Modify: `frontend/src/app/layout.tsx`
- Add: frontend browser test configuration and two demo-path tests

- [ ] Put order setup before the empty phone and replace the empty decision area with a three-step cue.
- [ ] Make a successful confirmation the dominant state and remove stale rejection evidence.
- [ ] Fit three alternatives in one desktop view and label deterministic fallback honestly.
- [ ] Verify happy and difficult paths in a headed browser at desktop and mobile widths.

### Task 4: One truthful submission story

**Files:**
- Modify: `README.md`
- Modify: `slides.md`
- Modify: `frontend/src/app/about/page.tsx`
- Modify: `CLAUDE.md`
- Modify: `.env.example`
- Create: `.github/workflows/ci.yml`

- [ ] Rewrite the opening around Floof's timing problem and the judged loop.
- [ ] Replace conflicting volumes, days, brands, test counts and hard-coded timings with verified facts or labelled demo assumptions.
- [ ] Add concise Mermaid agent and system diagrams plus a focused sponsor-fit section.
- [ ] Add CI for the full Python suite, TypeScript and the production frontend build.
- [ ] Run claim searches, all tests, typecheck, build and final browser QA.

### Task 5: Review and pull request

- [ ] Review the complete diff against the spec and remove unrelated changes.
- [ ] Request independent code, test-gap and security review.
- [ ] Fix all critical and important findings.
- [ ] Commit scoped changes, push the feature branch and open a PR without merging.
