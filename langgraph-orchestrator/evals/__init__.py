"""Phoenix eval harness for the ACI agent.

Measures things that stay stable over time — routing, skill/tool selection,
groundedness of the numbers in the answer against the tool output captured in
the SAME run. Live fabric facts (endpoint counts, health scores, fault totals)
are never frozen as ground truth.

Entry point: ``python -m evals.run`` from the langgraph-orchestrator directory.
"""
