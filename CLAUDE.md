# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

Tradeoff: These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding
Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

State your assumptions explicitly. If uncertain, ask.
If multiple interpretations exist, present them - don't pick silently.
If a simpler approach exists, say so. Push back when warranted.
If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First
Minimum code that solves the problem. Nothing speculative.

No features beyond what was asked.
No abstractions for single-use code.
No "flexibility" or "configurability" that wasn't requested.
No error handling for impossible scenarios.
If you write 200 lines and it could be 50, rewrite it.
Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes
Touch only what you must. Clean up only your own mess.

When editing existing code:

Don't "improve" adjacent code, comments, or formatting.
Don't refactor things that aren't broken.
Match existing style, even if you'd do it differently.
If you notice unrelated dead code, mention it - don't delete it.
When your changes create orphans:

Remove imports/variables/functions that YOUR changes made unused.
Don't remove pre-existing dead code unless asked.
The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution
Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

"Add validation" → "Write tests for invalid inputs, then make them pass"
"Fix the bug" → "Write a test that reproduces it, then make it pass"
"Refactor X" → "Ensure tests pass before and after"
For multi-step tasks, state a brief plan:

1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

These guidelines are working if: fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## Repository Contents

This directory contains a single research paper:
- `ibac-paper.pdf` — "Intent-Based Access Control: Securing Agentic AI Through Fine-Grained Authorization of User Intent" by Jordan Potti

There is no executable code, build system, or test suite here.

## Paper Overview

IBAC is an authorization framework that secures AI agents against prompt injection attacks by externalizing permission decisions away from the LLM's own reasoning.

**Core idea**: Before an agent executes any tool, a dedicated LLM call parses the user's original intent and constructs a minimal set of authorization tuples in OpenFGA. Every subsequent tool invocation is checked against these tuples — the model cannot grant itself new permissions mid-execution, even under injection.

## Architecture (6-Component Pipeline)

1. **Request Context Assembly** — collects trusted identifiers (contacts, file paths) from the environment
2. **Intent Parser** — separate LLM call that extracts the minimum capabilities needed to fulfill the user's request
3. **Tuple Construction** — maps parsed capabilities to OpenFGA relationship tuples `(user, can_invoke, tool_invocation)`
4. **Unified Authorization with Deny Policies** — FGA check at every tool call; deny rules block injected scope expansions
5. **`invokeToolWithAuth` wrapper** — enforces the FGA check before dispatching to the actual tool
6. **Escalation Protocol** — surfaces concrete, human-readable prompts when a tool call is denied but potentially legitimate

## Key Design Decisions

- **Strict mode**: minimal permissions per request → 100% security, 33.3% utility (AgentDojo benchmark, 240 injection runs)
- **Permissive mode**: broader per-request permissions → 98.8% security, 65.8% utility
- Authorization overhead: ~8–9 ms per tool invocation; intent parsing adds ~5.5–6.8 s per request
- Reference implementation: TypeScript + OpenFGA + Claude Sonnet

## Relation to Other Approaches

- **CaMeL** (Google DeepMind): dual-LLM with custom interpreter; 77% utility with provable security — higher utility ceiling but harder to retrofit
- **IBAC**: single extra LLM call + lightweight FGA; easier to add to existing agent architectures
