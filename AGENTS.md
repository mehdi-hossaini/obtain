# Unix Philosophy: Do One Thing and Do It Well

## Guiding Principle

When working in this directory, pursue a single, well-defined objective and deliver it thoroughly. Build tools and components with clear responsibilities that work well together. Resist turning a focused solution into a monolithic "super app."

Apply this principle at the right level: a task may require changes across several files or components, and an application may coordinate several capabilities. What matters is that each part has a clear purpose and every change serves the requested outcome.

## Define the Task

- Identify the problem, the intended behavior, and the conditions that make the work complete.
- Use the user's request and the existing project context to establish scope. Ask for clarification when ambiguity would materially change the result.
- Separate required work from optional improvements. Complete the dependencies needed for the requested behavior, but leave unrelated features and cleanup for separate tasks.
- Avoid adding speculative requirements, extension points, or configuration for hypothetical future needs.

## Understand Before Changing

- Read the relevant code, documentation, and applicable instructions before editing.
- Look for existing functionality to reuse or extend before introducing another implementation.
- Follow established project conventions unless they prevent a correct solution.
- Trace the behavior far enough to address its cause and understand affected callers.

## Give Each Component a Clear Purpose

- Keep tools, modules, functions, and interfaces focused on coherent responsibilities.
- Separate concerns when they change independently or obscure one another, such as core logic, storage, and presentation.
- Keep dependencies explicit and avoid hidden coupling through shared mutable state.
- Prefer a cohesive module over excessive fragmentation. A clear responsibility does not require a separate package, service, or process.
- Introduce abstractions when they clarify an existing boundary or remove meaningful duplication; keep them concrete and easy to follow.

## Design for Composition

- Use small, explicit interfaces with understandable inputs, outputs, and error behavior.
- Make outputs useful as inputs to other components where that supports the task.
- Prefer established formats and conventions over custom protocols when they fit the problem.
- Keep core behavior usable without unnecessary dependence on a particular interface or execution environment.
- For command-line tools, support standard input and output where useful, keep diagnostics on standard error, and return meaningful exit codes.

## Choose the Simplest Complete Solution

- Prefer straightforward implementations with predictable control flow and data flow.
- Add dependencies, layers, configuration, and frameworks only when they solve a concrete problem better than the existing approach.
- Keep changes easy to review. Avoid broad rewrites or unrelated formatting changes while delivering a focused task.
- Preserve established behavior and public interfaces unless the task requires changing them.
- When replacing an implementation, remove obsolete paths within the affected scope so competing sources of truth do not remain.

## Make the Focused Behavior Reliable

- Handle the relevant edge cases, failure modes, and resource cleanup needed for the task.
- Validate input at appropriate boundaries and make failures actionable without exposing sensitive information.
- Fix underlying causes rather than hiding errors or silently substituting success.
- Consider performance, security, and accessibility where they affect the requested behavior; keep the response proportional to the actual needs.
- Document assumptions and tradeoffs that a future maintainer needs to understand.

## Verify the Outcome

- Run the relevant project checks and verify the intended behavior at the appropriate level.
- Add or update tests when they meaningfully protect changed behavior, especially bug fixes, important edge cases, and interactions between components.
- Prefer tests of observable behavior over tests that merely repeat implementation details.
- Review the final changes for unintended behavior, unnecessary complexity, and scope drift.
- Report what changed, what was verified, and any remaining limitations. Distinguish completed checks from checks that could not be run.

## Know When to Stop

The work is complete when the requested behavior works, necessary related changes are finished, appropriate verification is done, and material limitations are clearly reported.

Do not expand a completed task merely because adjacent features are possible. Leave optional ideas as brief follow-up suggestions when useful. Optimize for a focused solution that is correct, understandable, and easy to maintain.
