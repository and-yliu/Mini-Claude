---
name: orchestrate
description: Complete task with a planner, executor, and reviewer workflow
allowed_tools:
  - subagent
  - agent_result
  - task_create
  - task_update
  - task_list
---
You are a multi-agent coordinator. Complete the following goal using the three stages below:

$ARGUMENTS

Follow these stages in order:

## Stage 1: Plan

Call the subagent tool with:
- description: "Plan task"
- subagent_type: "planner"
- prompt: the complete original goal, asking for an ordered implementation plan whose steps
  each have explicit success criteria

## Stage 2: Execute

Call the subagent tool with:
- description: "Execute plan"
- subagent_type: "executor"
- prompt: the original goal plus the planner's complete output, asking the executor to perform
  each step and report its result

## Stage 3: Review

Call the subagent tool with:
- description: "Review result"
- subagent_type: "reviewer"
- prompt: the original goal plus the executor's complete output, asking the reviewer to verify
  the actual result, identify omissions, and assess whether the goal was achieved

After all three stages, report:
1. A summary of the plan
2. A summary of the execution and artifacts produced
3. The review conclusion
4. Whether the overall task succeeded and any remaining issues
