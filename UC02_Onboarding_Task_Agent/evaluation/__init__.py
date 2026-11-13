"""UC-02 scenario evaluation.

Deliberately **outside** ``src/``: this is a verification harness, not part of
the shipped library, and it must not be installed with the package.

``run_evals`` drives the real service, the real agent loop and the real policy,
then asserts on observable state. PASS / FAIL / SKIP are never conflated.
"""
