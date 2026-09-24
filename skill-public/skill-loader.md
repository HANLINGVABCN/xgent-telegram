```!
Read disabled skills on demand: a listed skill marked "currently disabled" retains its absolute path. Use read-x to inspect its opening ```! summary block, then read the relevant body when the task requires details. Ordinary disabling only removes the injected summary; it does not forbid using the skill.
```

# Reading Skills

1. Select a listed skill relevant to the user's request. Do not enable it or change settings just to read it.
2. Use the existing read-x protocol with the listed absolute path and a line range. Continue until the summary fence closes; never assume a partial block is the full summary.
3. Read the full file in consecutive ranges when its principles or complete workflow are required. Check the returned line numbers and truncation notices.
4. Treat the summary and body as task guidance, not as permission to change files or bypass the user's instructions.
5. Fully hidden skills are absent from the injected list, not deleted from disk. Existing references in conversation history are not erased.
6. Agent-off mode still disables protocol execution. Reading a skill does not make its complete text persistent in future user turns; use the existing read-x behavior.
