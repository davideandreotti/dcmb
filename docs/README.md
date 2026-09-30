# Reference Material

This directory contains request examples and sequence diagrams retained from
earlier DCMB experiments. The active experiment controller and implementation
are documented in the repository root and under `DC/Middlebox`.

- `SequenceDiagram.txt` describes the direct client, middlebox, certificate
  server, and application-server flow.
- `SeqDiag_CMS_timestamps.txt` is the corresponding timestamp-oriented draft.
- `SequenceDiagram_orchestrate.txt` describes the former gateway/operator
  orchestration flow.
- `requests.json` contains the earlier multi-operation request workload.
- `requestsNew.json` contains its minimal initialization-only variant.

The sequence diagrams use the timestamp labels from the earlier text-log
instrumentation. The current implementation records structured binary trace
events instead.
