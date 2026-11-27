# Repository instructions

- This repository provides on-demand GitHub PR reviews with OpenCode and CSCS inference.
- Keep changes scoped and preserve authorization, credential separation, read-only source mounts, and SHA/digest pins.
- For code changes, run the relevant local checks listed in README.md.
- Do not commit, push, publish releases, or run live inference unless requested.

## Markdown

- Exclude Markdown files under `image/` from formatting and prose cleanup; they are baked into the runtime image and changes require an image rebuild.

- Write one sentence per source line; do not wrap sentences across lines.
- Use concise, technical language without flowery prose.
- Name files, symbols, conditions, and consequences explicitly.
- State each fact once and remove redundant text.
- Keep claims consistent with code and distinguish enforced controls, policy, and future work.
- Preserve code blocks, tables, links, and list nesting when splitting prose into lines.
