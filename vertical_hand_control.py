"""Production import for the versioned vertical hand-look controller.

The public module path and ``VerticalHandController`` class remain stable for
ControlKernel and older integrations.  The implementation lives in the
versioned module so future experiments can be reviewed without replacing the
production adapter or silently changing its import contract.
"""

from vertical_hand_control_v160 import VerticalHandController

__all__ = ["VerticalHandController"]
