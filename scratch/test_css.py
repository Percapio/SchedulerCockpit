import sys
from PyQt6.QtWidgets import QApplication, QLabel
app = QApplication(sys.argv)
l = QLabel('Test')
l.setStyleSheet('padding-left: 3%;')
print(l.styleSheet())
print(l.geometry())
