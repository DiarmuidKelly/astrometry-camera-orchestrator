"""Application layer — use-case services composing the domain ports.

Depends on domain ports and value objects only; concrete adapters are injected
by the composition root. Contains no argparse/stdout/HTTP concerns.
"""
