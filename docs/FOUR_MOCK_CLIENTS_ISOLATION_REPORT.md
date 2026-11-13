# Four existing mock clients: cross-client isolation report

Date: 2026-11-04. All results below come from **local mock tests** (`UC02_Onboarding_Task_Agent/tests/test_four_mock_clients.py`,
`tests/test_existing_clients_retrieval.py`) on the schema-enforcing mock database, which starts from a read-only snapshot of the live configuration rows and
then has the four profiles seeded into it. **Nothing was written to Supabase.** The live database has not been seeded, so live isolation of the new
configuration is not yet tested.

| Check | Result | How |
|---|---|---|
| Each client's checklist contains only its own task templates | PASS (4/4) | every included `template_key` is in the client's own profile |
| Each client's documents are only its own | PASS (4/4) | retrieval and UC-02 document selection |
| Calendars and holidays never mix | PASS (4/4) | each client's calendar equals exactly its own holiday dates |
| CLIENT_INDIA never receives CLIENT_DUBAI-only tasks (visa, Emirates ID, residency, medical fitness) | PASS | set difference is empty |
| CLIENT_INDIA domestic employees never receive a visa/medical/biometrics task (3 roles) | PASS | no such category in the checklist |
| A client cannot preview another client's checklist | PASS | `DataIsolationError` |
| A client without its own templates never borrows another's | PASS | earlier suite (each existing client made approved in the mock copy is refused, never served SYN-UAE-TEST's rows) |
| Version 1 draft rows are never mixed with version 2 | PASS | the reader serves version 2 for all four |
| Missing / invalid / unknown client ids | PASS | `client is not in client_configs`; no fallback |
| A mock client without the runtime's synthetic opt-in | PASS | refused (`marked mock`) |
| Retrieval and the UC-02 checklist agree task-for-task | PASS (4/4) | the same applicability rule in both |

Known test-double limitation: the mock compares strings case-insensitively, so case-sensitive client ids are proven only live (done earlier, read-only:
`client_dubai` is refused).
