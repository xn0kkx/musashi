"""MusashiOS host-side voice loop (V1).

Nothing heavy is imported here on purpose: `import musashi_voice` must stay
free of torch, sounddevice and faster-whisper so the tests, the config loader
and the vsock client are usable on a machine with no audio stack at all.
"""
__version__ = "0.1.0"
