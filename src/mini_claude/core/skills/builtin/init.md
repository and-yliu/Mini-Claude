---
name: init
description: Analyze the current project and create .mini/context.md
allowed_tools:
  - read_file
  - list_dir
  - write_file
  - bash
---
You are a project-analysis specialist. Analyze the current project and create a
`.mini/context.md` file that helps AI agents quickly understand it in future sessions.

Analysis steps:
1. Use list_dir to explore the root and important subdirectories.
2. Read relevant project files such as README, package.json, pyproject.toml, or Cargo.toml.
3. Identify the languages, frameworks, main modules, and directory structure.

The context file must include:
- Project name and a one-sentence description
- Technology stack
- Important directories
- Common development commands for building, testing, and running
- Important conventions or restrictions

Write the result to `.mini/context.md`. Create `.mini/` first if necessary.

$ARGUMENTS
