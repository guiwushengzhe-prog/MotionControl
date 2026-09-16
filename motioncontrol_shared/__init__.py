"""Code shared by the MotionControl desktop app and the cloud service.

Hard rule, enforced by tests/test_shared_boundary.py and by the Linux CI job:
this package depends on the Python standard library and nothing else.  No
Windows APIs, no file writes, no network, no environment lookups, no FastAPI or
SQLAlchemy, and no import of a desktop module.  The dependency arrow is
``desktop -> shared <- cloud`` and never ``cloud -> desktop``.

The reason is concrete: ``output_backend.py`` imports ``ctypes.wintypes``, which
raises on Linux, and it is reachable from ``server.py``, ``control_kernel.py``
and ``voice_backend.py``.  Anything the cloud needs has to live here instead.
"""
