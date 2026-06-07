import sys
from PyQt6.QtWidgets import QApplication, QLineEdit
app = QApplication(sys.argv)
l = QLineEdit('Test')
l.setStyleSheet('margin-right: 3%;')
print(l.styleSheet())
