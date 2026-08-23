# CAPE-project Suricata rules

This directory holds CAPE-project curated rules that get merged with ET Open
during the suricata-rules-build workflow. Add files as `*.rules`.

Each rule should:
- Use a sid range we control (start with sid:8000000 to avoid ET conflicts).
- Carry a clear msg: prefix like `CAPE`.
- Include a rev: counter that increments on edits.

