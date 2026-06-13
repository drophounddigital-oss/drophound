"""The automation stack: monitors -> resale -> digest -> alerts, run by pipeline.

This is the in-code equivalent of the plan's n8n/Make orchestration. Everything
degrades to a safe offline/dry-run mode when no API keys are configured.
"""
