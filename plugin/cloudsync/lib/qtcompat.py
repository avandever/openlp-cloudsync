"""
Qt binding compatibility shim.

Released OpenLP versions (3.1.x) embed PyQt5, while the current development
tree uses PySide6.  Import the Qt modules through this shim so the plugin
works with whichever binding the host application provides::

    from .qtcompat import QtCore, QtGui, QtWidgets
"""
import logging

log = logging.getLogger(__name__)

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    QT_BINDING = 'PySide6'
except ImportError:
    from PyQt5 import QtCore, QtGui, QtWidgets
    QT_BINDING = 'PyQt5'

# Normalise the signal/slot factory names: PyQt5 spells them ``pyqtSignal`` /
# ``pyqtSlot`` while PySide6 uses ``Signal`` / ``Slot``.
if not hasattr(QtCore, 'Signal'):
    QtCore.Signal = QtCore.pyqtSignal
if not hasattr(QtCore, 'Slot'):
    QtCore.Slot = QtCore.pyqtSlot

log.debug(f'CloudSync: using Qt binding {QT_BINDING}')
