"""MusashiOS capability daemon.

The single process in the guest that holds real effectors. Proposers (gesture
engine, and from V1 the host's voice loop over vsock) send intents; this
daemon validates them against its own Registry and decides. The LLM proposes;
the daemon decides.
"""
__version__ = "0.1.0"
