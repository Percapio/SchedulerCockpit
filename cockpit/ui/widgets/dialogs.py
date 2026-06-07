from PyQt6.QtWidgets import QMessageBox

def confirm_destructive(title: str, body: str, confirm_label: str) -> bool:
    """
    Shows a modal dialog with a custom confirmation button and a Cancel button.
    The Cancel button is the default. Returns True only if the confirm button is clicked.
    """
    msg = QMessageBox()
    msg.setWindowTitle(title)
    msg.setText(body)
    msg.setIcon(QMessageBox.Icon.Warning)

    confirm_btn = msg.addButton(confirm_label, QMessageBox.ButtonRole.AcceptRole)
    cancel_btn = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    
    msg.setDefaultButton(cancel_btn)

    msg.exec()
    
    return msg.clickedButton() == confirm_btn
